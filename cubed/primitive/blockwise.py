import inspect
import itertools
import logging
import math
from collections.abc import Iterator
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import toolz
import zarr
from toolz import map

from cubed.backend_array_api import (
    backend_array_to_numpy_array,
    numpy_array_to_backend_array,
)
from cubed.runtime.types import CubedPipeline
from cubed.storage.zarr import T_ZarrArray, lazy_zarr_array
from cubed.types import T_Chunks, T_DType, T_Shape, T_Store
from cubed.utils import chunk_memory, get_item, map_nested, split_into, to_chunksize
from cubed.vendor.dask.array.core import normalize_chunks
from cubed.vendor.dask.blockwise import _get_coord_mapping, _make_dims, lol_product
from cubed.vendor.dask.core import flatten

from .types import CubedArrayProxy, MemoryModeller, PrimitiveOperation

logger = logging.getLogger(__name__)


sym_counter = 0


def gensym(name: str) -> str:
    global sym_counter
    sym_counter += 1
    return f"{name}-{sym_counter:03}"


@dataclass(frozen=True)
class BlockwiseSpec:
    """Specification for how to run blockwise on an array.

    This is similar to ``CopySpec`` in rechunker.

    Attributes
    ----------
    block_function : Callable
        A function that maps an output chunk index to one or more input chunk indexes.
    function : Callable
        A function that maps input chunks to an output chunk.
    function_nargs: int
        The number of array arguments that ``function`` takes.
    num_input_blocks: Tuple[int, ...]
        The number of input blocks read from each input array.
    reads_map : Dict[str, CubedArrayProxy]
        Read proxy dictionary keyed by array name.
    writes_list : List[CubedArrayProxy]
        Write proxy list where entries have an ``array`` attribute that supports ``__setitem__``.
    """

    block_function: Callable[..., Any]
    function: Callable[..., Any]
    function_nargs: int
    num_input_blocks: Tuple[int, ...]
    reads_map: Dict[str, CubedArrayProxy]
    writes_list: List[CubedArrayProxy]


def apply_blockwise(out_key: List[int], *, config: BlockwiseSpec) -> None:
    """Stage function for blockwise."""
    # lithops needs params to be lists not tuples, so convert back
    out_key_tuple = tuple(out_key)

    # get array chunks for input keys, preserving any nested list structure
    args = []
    get_chunk_config = partial(get_chunk, config=config)
    name_chunk_inds = config.block_function(("out",) + out_key_tuple)
    for name_chunk_ind in name_chunk_inds:
        arg = map_nested(get_chunk_config, name_chunk_ind)
        args.append(arg)

    results = config.function(*args)
    # if blockwise function is a regular function (not a generator) then make it iterable
    if not inspect.isgeneratorfunction(config.function):
        results = (results,)
    for i, result in enumerate(results):
        out_chunk_key = key_to_slices(
            out_key_tuple, config.writes_list[i].array, config.writes_list[i].chunks
        )
        if isinstance(result, dict):  # structured array with named fields
            for k, v in result.items():
                v = backend_array_to_numpy_array(v)
                config.writes_list[i].open().set_basic_selection(
                    out_chunk_key, v, fields=k
                )
        else:
            result = backend_array_to_numpy_array(result)
            config.writes_list[i].open()[out_chunk_key] = result


def key_to_slices(
    key: Tuple[int, ...], arr: T_ZarrArray, chunks: Optional[T_Chunks] = None
) -> Tuple[slice, ...]:
    """Convert a chunk index key to a tuple of slices"""
    chunks = normalize_chunks(chunks or arr.chunks, shape=arr.shape, dtype=arr.dtype)
    return get_item(chunks, key)


def get_chunk(name_chunk_ind, config):
    """Read a chunk from the named array"""
    name = name_chunk_ind[0]
    chunk_ind = name_chunk_ind[1:]
    arr = config.reads_map[name].open()
    chunk_key = key_to_slices(chunk_ind, arr)
    arg = arr[chunk_key]
    arg = numpy_array_to_backend_array(arg)
    return arg


def blockwise(
    func: Callable[..., Any],
    out_ind: Sequence[Union[str, int]],
    *args: Any,
    allowed_mem: int,
    reserved_mem: int,
    target_store: T_Store,
    target_path: Optional[str] = None,
    shape: T_Shape,
    dtype: T_DType,
    chunks: T_Chunks,
    new_axes: Optional[Dict[int, int]] = None,
    in_names: Optional[List[str]] = None,
    out_name: Optional[str] = None,
    extra_projected_mem: int = 0,
    extra_func_kwargs: Optional[Dict[str, Any]] = None,
    fusable: bool = True,
    num_input_blocks: Optional[Tuple[int, ...]] = None,
    **kwargs,
) -> PrimitiveOperation:
    """Apply a function to multiple blocks from multiple inputs, expressed using concise indexing rules.

    Unlike ```general_blockwise``, an index notation is used to specify the block mapping,
    like in Dask Array.

    Parameters
    ----------
    func : callable
        Function to apply to individual tuples of blocks
    out_ind : iterable
        Block pattern of the output, something like 'ijk' or (1, 2, 3)
    *args : sequence of Array, index pairs
        Sequence like (x, 'ij', y, 'jk', z, 'i')
    allowed_mem : int
        The memory available to a worker for running a task, in bytes. Includes ``reserved_mem``.
    reserved_mem : int
        The memory reserved on a worker for non-data use when running a task, in bytes
    target_store : string or zarr.Array
        Path to output Zarr store, or Zarr array
    shape : tuple
        The shape of the output array.
    dtype : np.dtype
        The ``dtype`` of the output array.
    chunks : tuple
        The chunks of the output array.
    new_axes : dict
        New indexes and their dimension lengths
    extra_projected_mem : int
        Extra memory projected to be needed (in bytes) in addition to the memory used reading
        the input arrays and writing the output.
    extra_func_kwargs : dict
        Extra keyword arguments to pass to function that can't be passed as regular keyword arguments
        since they clash with other blockwise arguments (such as dtype).
    **kwargs : dict
        Extra keyword arguments to pass to function

    Returns
    -------
    CubedPipeline to run the operation
    """

    arrays: Sequence[T_ZarrArray] = args[::2]
    array_names = in_names or [f"in_{i}" for i in range(len(arrays))]

    inds: Sequence[Union[str, int]] = args[1::2]

    numblocks: Dict[str, Tuple[int, ...]] = {}
    for name, array in zip(array_names, arrays):
        input_chunks = normalize_chunks(
            array.chunks, shape=array.shape, dtype=array.dtype
        )
        numblocks[name] = tuple(map(len, input_chunks))

    argindsstr: List[Any] = []
    for name, ind in zip(array_names, inds):
        argindsstr.extend((name, ind))

    block_function = make_blockwise_function_flattened(
        func,
        out_name or "out",
        out_ind,
        *argindsstr,
        numblocks=numblocks,
        new_axes=new_axes,
    )

    return general_blockwise(
        func,
        block_function,
        *arrays,
        allowed_mem=allowed_mem,
        reserved_mem=reserved_mem,
        target_stores=[target_store],
        target_paths=[target_path] if target_path is not None else None,
        shapes=[shape],
        dtypes=[dtype],
        chunkss=[chunks],
        in_names=in_names,
        extra_projected_mem=extra_projected_mem,
        extra_func_kwargs=extra_func_kwargs,
        fusable=fusable,
        num_input_blocks=num_input_blocks,
        **kwargs,
    )


def general_blockwise(
    func: Callable[..., Any],
    block_function: Callable[..., Any],
    *arrays: Any,
    allowed_mem: int,
    reserved_mem: int,
    target_stores: List[T_Store],
    target_paths: Optional[List[str]] = None,
    shapes: List[T_Shape],
    dtypes: List[T_DType],
    chunkss: List[T_Chunks],
    in_names: Optional[List[str]] = None,
    extra_projected_mem: int = 0,
    extra_func_kwargs: Optional[Dict[str, Any]] = None,
    fusable: bool = True,
    num_input_blocks: Optional[Tuple[int, ...]] = None,
    **kwargs,
) -> PrimitiveOperation:
    """A more general form of ``blockwise`` that uses a function to specify the block
    mapping, rather than an index notation, and which supports multiple outputs.

    Parameters
    ----------
    func : callable
        Function to apply to individual tuples of blocks
    block_function : callable
        A function that maps an output chunk index to one or more input chunk indexes.
    *arrays : sequence of Array
        The input arrays.
    allowed_mem : int
        The memory available to a worker for running a task, in bytes. Includes ``reserved_mem``.
    reserved_mem : int
        The memory reserved on a worker for non-data use when running a task, in bytes
    target_store : string or zarr.Array
        Path to output Zarr store, or Zarr array
    shape : tuple
        The shape of the output array.
    dtype : np.dtype
        The ``dtype`` of the output array.
    chunks : tuple
        The chunks of the output array.
    extra_projected_mem : int
        Extra memory projected to be needed (in bytes) in addition to the memory used reading
        the input arrays and writing the output.
    extra_func_kwargs : dict
        Extra keyword arguments to pass to function that can't be passed as regular keyword arguments
        since they clash with other blockwise arguments (such as dtype).
    **kwargs : dict
        Extra keyword arguments to pass to function

    Returns
    -------
    PrimitiveOperation to run the operation
    """
    array_names = in_names or [f"in_{i}" for i in range(len(arrays))]
    array_map = {name: array for name, array in zip(array_names, arrays)}

    func_kwargs = extra_func_kwargs or {}
    func_with_kwargs = partial(func, **{**kwargs, **func_kwargs})
    num_input_blocks = num_input_blocks or (1,) * len(arrays)
    read_proxies = {
        name: CubedArrayProxy(array, array.chunks) for name, array in array_map.items()
    }

    write_proxies = []
    output_chunk_memory = 0
    target_array = []

    for i, target_store in enumerate(target_stores):
        chunks_normal = normalize_chunks(chunkss[i], shape=shapes[i], dtype=dtypes[i])
        chunksize = to_chunksize(chunks_normal)
        if isinstance(target_store, zarr.Array):
            ta = target_store
        else:
            ta = lazy_zarr_array(
                target_store,
                shapes[i],
                dtype=dtypes[i],
                chunks=chunksize,
                path=target_paths[i] if target_paths is not None else None,
            )
        target_array.append(ta)

        write_proxies.append(CubedArrayProxy(ta, chunksize))

        # only one output chunk is read into memory at a time, so we find the largest
        output_chunk_memory = max(
            output_chunk_memory, chunk_memory(dtypes[i], chunksize) * 2
        )

    if len(target_array) == 1:
        target_array = target_array[0]

    spec = BlockwiseSpec(
        block_function,
        func_with_kwargs,
        len(arrays),
        num_input_blocks,
        read_proxies,
        write_proxies,
    )

    # calculate projected memory
    projected_mem = reserved_mem + extra_projected_mem
    # inputs
    for array in arrays:  # inputs
        # memory for a compressed and an uncompressed input array chunk
        # - we assume compression has no effect (so it's an overestimate)
        # - ideally we'd be able to look at nbytes_stored,
        #   but this is not possible in general since the array has not been written yet
        projected_mem += chunk_memory(array.dtype, array.chunks) * 2
    # output
    # memory for a compressed and an uncompressed output array chunk
    # - this assumes the blockwise function creates a new array)
    # - numcodecs uses a working output buffer that's the size of the array being compressed
    projected_mem += output_chunk_memory

    if projected_mem > allowed_mem:
        raise ValueError(
            f"Projected blockwise memory ({projected_mem}) exceeds allowed_mem ({allowed_mem}), including reserved_mem ({reserved_mem})"
        )

    # this must be an iterator of lists, not of tuples, otherwise lithops breaks
    output_blocks = map(
        list, itertools.product(*[range(len(c)) for c in chunks_normal])
    )
    num_tasks = math.prod(len(c) for c in chunks_normal)

    pipeline = CubedPipeline(
        apply_blockwise,
        gensym("apply_blockwise"),
        output_blocks,
        spec,
    )
    return PrimitiveOperation(
        pipeline=pipeline,
        source_array_names=array_names,
        target_array=target_array,
        projected_mem=projected_mem,
        allowed_mem=allowed_mem,
        reserved_mem=reserved_mem,
        num_tasks=num_tasks,
        fusable=fusable,
    )


# Code for fusing blockwise operations


def is_fuse_candidate(primitive_op: PrimitiveOperation) -> bool:
    """
    Return True if a primitive operation is a candidate for blockwise fusion.
    """
    return primitive_op.pipeline.function == apply_blockwise


def can_fuse_primitive_ops(
    primitive_op1: PrimitiveOperation, primitive_op2: PrimitiveOperation
) -> bool:
    if is_fuse_candidate(primitive_op1) and is_fuse_candidate(primitive_op2):
        return primitive_op1.num_tasks == primitive_op2.num_tasks
    return False


def can_fuse_multiple_primitive_ops(
    name: str,
    primitive_op: PrimitiveOperation,
    predecessor_primitive_ops: List[PrimitiveOperation],
    *,
    max_total_num_input_blocks: Optional[int] = None,
) -> bool:
    if is_fuse_candidate(primitive_op) and all(
        is_fuse_candidate(p) for p in predecessor_primitive_ops
    ):
        # If the peak projected memory for running all the predecessor ops in
        # order is larger than allowed_mem then we can't fuse.
        peak_projected = peak_projected_mem(predecessor_primitive_ops)
        if peak_projected > primitive_op.allowed_mem:
            logger.debug(
                "can't fuse %s since peak projected memory for predecessor ops (%s) is greater than allowed (%s)",
                name,
                peak_projected,
                primitive_op.allowed_mem,
            )
            return False
        # If the number of input blocks for each input is not uniform, then we
        # can't fuse. (This should never happen since all operations are
        # currently uniform, and fused operations are too if fuse is applied in
        # topological order.)
        num_input_blocks = primitive_op.pipeline.config.num_input_blocks
        if not all(num_input_blocks[0] == n for n in num_input_blocks):
            logger.debug(
                "can't fuse %s since number of input blocks for each input is not uniform: %s",
                name,
                num_input_blocks,
            )
            return False
        if max_total_num_input_blocks is None:
            # If max total input blocks not specified, then only fuse if num
            # tasks of predecessor ops match.
            ret = all(
                primitive_op.num_tasks == p.num_tasks for p in predecessor_primitive_ops
            )
            if ret:
                logger.debug(
                    "can fuse %s since num tasks of predecessor ops match", name
                )
            else:
                logger.debug(
                    "can't fuse %s since num tasks of predecessor ops do not match",
                    name,
                )
            return ret
        else:
            total_num_input_blocks = 0
            for ni, p in zip(num_input_blocks, predecessor_primitive_ops):
                for nj in p.pipeline.config.num_input_blocks:
                    total_num_input_blocks += ni * nj
            ret = total_num_input_blocks <= max_total_num_input_blocks
            if ret:
                logger.debug(
                    "can fuse %s since total number of input blocks (%s) does not exceed max (%s)",
                    name,
                    total_num_input_blocks,
                    max_total_num_input_blocks,
                )
            else:
                logger.debug(
                    "can't fuse %s since total number of input blocks (%s) exceeds max (%s)",
                    name,
                    total_num_input_blocks,
                    max_total_num_input_blocks,
                )
            return ret
    logger.debug(
        "can't fuse %s since primitive op and predecessors are not all candidates", name
    )
    return False


def peak_projected_mem(primitive_ops):
    """Calculate the peak projected memory for running a series of primitive ops
    and retaining their return values in memory."""
    memory_modeller = MemoryModeller()
    for p in primitive_ops:
        memory_modeller.allocate(p.projected_mem)
        chunkmem = chunk_memory(p.target_array.dtype, p.target_array.chunks)
        memory_modeller.free(p.projected_mem - chunkmem)
    return memory_modeller.peak_mem


def fuse(
    primitive_op1: PrimitiveOperation, primitive_op2: PrimitiveOperation
) -> PrimitiveOperation:
    """
    Fuse two blockwise operations into a single operation, avoiding writing to (or reading from) the target of the first operation.
    """

    assert primitive_op1.num_tasks == primitive_op2.num_tasks

    pipeline1 = primitive_op1.pipeline
    pipeline2 = primitive_op2.pipeline

    mappable = pipeline2.mappable

    def fused_blockwise_func(out_key):
        return pipeline1.config.block_function(
            *pipeline2.config.block_function(out_key)
        )

    def fused_func(*args):
        return pipeline2.config.function(pipeline1.config.function(*args))

    function_nargs = pipeline1.config.function_nargs
    read_proxies = pipeline1.config.reads_map
    write_proxies = pipeline2.config.writes_list
    num_input_blocks = tuple(
        n * pipeline2.config.num_input_blocks[0]
        for n in pipeline1.config.num_input_blocks
    )
    spec = BlockwiseSpec(
        fused_blockwise_func,
        fused_func,
        function_nargs,
        num_input_blocks,
        read_proxies,
        write_proxies,
    )

    source_array_names = primitive_op1.source_array_names
    target_array = primitive_op2.target_array
    projected_mem = max(primitive_op1.projected_mem, primitive_op2.projected_mem)
    allowed_mem = primitive_op2.allowed_mem
    reserved_mem = primitive_op2.reserved_mem
    num_tasks = primitive_op2.num_tasks

    pipeline = CubedPipeline(
        apply_blockwise,
        gensym("fused_apply_blockwise"),
        mappable,
        spec,
    )
    return PrimitiveOperation(
        pipeline=pipeline,
        source_array_names=source_array_names,
        target_array=target_array,
        projected_mem=projected_mem,
        allowed_mem=allowed_mem,
        reserved_mem=reserved_mem,
        num_tasks=num_tasks,
        fusable=True,
    )


def fuse_multiple(
    primitive_op: PrimitiveOperation, *predecessor_primitive_ops: PrimitiveOperation
) -> PrimitiveOperation:
    """
    Fuse a blockwise operation and its predecessors into a single operation, avoiding writing to (or reading from) the targets of the predecessor operations.
    """

    pipeline = primitive_op.pipeline
    predecessor_pipelines = [
        primitive_op.pipeline if primitive_op is not None else None
        for primitive_op in predecessor_primitive_ops
    ]

    # if a predecessor has no primitive op then use 1 for nargs
    predecessor_funcs_nargs = [
        pipeline.config.function_nargs if pipeline is not None else 1
        for pipeline in predecessor_pipelines
    ]

    mappable = pipeline.mappable

    def apply_pipeline_block_func(pipeline, n_input_blocks, arg):
        if pipeline is None:
            return (arg,)
        if n_input_blocks == 1:
            assert isinstance(arg, tuple)
            return pipeline.config.block_function(arg)
        else:
            # more than one input block is being read from arg
            assert isinstance(arg, (list, Iterator))
            if isinstance(arg, list):
                return tuple(
                    list(item)
                    for item in zip(*(pipeline.config.block_function(a) for a in arg))
                )
            else:
                # Return iterators to avoid materializing all array blocks at
                # once.
                return tuple(
                    iter(list(item))
                    for item in zip(*(pipeline.config.block_function(a) for a in arg))
                )

    def fused_blockwise_func(out_key):
        # this will change when multiple outputs are supported
        args = pipeline.config.block_function(out_key)
        # split all args to the fused function into groups, one for each predecessor function
        func_args = tuple(
            item
            for i, (p, a) in enumerate(zip(predecessor_pipelines, args))
            for item in apply_pipeline_block_func(
                p, pipeline.config.num_input_blocks[i], a
            )
        )
        return split_into(func_args, predecessor_funcs_nargs)

    def apply_pipeline_func(pipeline, n_input_blocks, *args):
        if pipeline is None:
            return args[0]
        if n_input_blocks == 1:
            ret = pipeline.config.function(*args)
        else:
            # More than one input block is being read from this group of args to primitive op.
            # Note that it is important that a list is not returned to avoid materializing all
            # array blocks at once.
            ret = map(lambda item: pipeline.config.function(*item), zip(*args))
        return ret

    def fused_func_single(*args):
        # args are grouped appropriately so they can be called by each predecessor function
        func_args = [
            apply_pipeline_func(p, pipeline.config.num_input_blocks[i], *a)
            for i, (p, a) in enumerate(zip(predecessor_pipelines, args))
        ]
        return pipeline.config.function(*func_args)

    # multiple outputs
    def fused_func_generator(*args):
        # args are grouped appropriately so they can be called by each predecessor function
        func_args = [
            apply_pipeline_func(p, pipeline.config.num_input_blocks[i], *a)
            for i, (p, a) in enumerate(zip(predecessor_pipelines, args))
        ]
        yield from pipeline.config.function(*func_args)

    fused_func = (
        fused_func_generator
        if inspect.isgeneratorfunction(pipeline.config.function)
        else fused_func_single
    )
    fused_function_nargs = pipeline.config.function_nargs
    # ok to get num_input_blocks[0] since it is uniform (see check in can_fuse_multiple_primitive_ops)
    fused_num_input_blocks = tuple(
        pipeline.config.num_input_blocks[0] * n
        for n in itertools.chain(
            *(
                p.pipeline.config.num_input_blocks if p is not None else (1,)
                for p in predecessor_primitive_ops
            )
        )
    )
    read_proxies = dict(pipeline.config.reads_map)
    for p in predecessor_pipelines:
        if p is not None:
            read_proxies.update(p.config.reads_map)
    write_proxies = pipeline.config.writes_list
    spec = BlockwiseSpec(
        fused_blockwise_func,
        fused_func,
        fused_function_nargs,
        fused_num_input_blocks,
        read_proxies,
        write_proxies,
    )

    source_array_names = []
    for i, p in enumerate(predecessor_primitive_ops):
        if p is None:
            source_array_names.append(primitive_op.source_array_names[i])
        else:
            source_array_names.extend(p.source_array_names)
    target_array = primitive_op.target_array
    projected_mem = max(
        primitive_op.projected_mem,
        peak_projected_mem(p for p in predecessor_primitive_ops if p is not None),
    )
    allowed_mem = primitive_op.allowed_mem
    reserved_mem = primitive_op.reserved_mem
    num_tasks = primitive_op.num_tasks

    fused_pipeline = CubedPipeline(
        apply_blockwise,
        gensym("fused_apply_blockwise"),
        mappable,
        spec,
    )
    return PrimitiveOperation(
        pipeline=fused_pipeline,
        source_array_names=source_array_names,
        target_array=target_array,
        projected_mem=projected_mem,
        allowed_mem=allowed_mem,
        reserved_mem=reserved_mem,
        num_tasks=num_tasks,
        fusable=True,
    )


# blockwise functions


def make_blockwise_function(
    func: Callable[..., Any],
    output: str,
    out_indices: Sequence[Union[str, int]],
    *arrind_pairs: Any,
    numblocks: Optional[Dict[str, Tuple[int, ...]]] = None,
    new_axes: Optional[Dict[int, int]] = None,
) -> Callable[[List[int]], Any]:
    """Make a function that is the equivalent of make_blockwise_graph."""

    if numblocks is None:
        raise ValueError("Missing required numblocks argument.")
    new_axes = new_axes or {}
    argpairs = list(toolz.partition(2, arrind_pairs))

    # Dictionary mapping {i: 3, j: 4, ...} for i, j, ... the dimensions
    dims = _make_dims(argpairs, numblocks, new_axes)

    # Generate the abstract "plan" before constructing
    # the actual graph
    (coord_maps, concat_axes, dummies) = _get_coord_mapping(
        dims,
        output,
        out_indices,
        numblocks,
        argpairs,
        False,
    )

    def blockwise_fn(out_key):
        out_coords = out_key[1:]

        # from Dask make_blockwise_graph
        deps = set()
        coords = out_coords + dummies
        args = []
        for cmap, axes, (arg, ind) in zip(coord_maps, concat_axes, argpairs):
            if ind is None:
                args.append(arg)
            else:
                arg_coords = tuple(coords[c] for c in cmap)
                if axes:
                    tups = lol_product((arg,), arg_coords)
                    deps.update(flatten(tups))
                else:
                    tups = (arg,) + arg_coords
                    deps.add(tups)
                args.append(tups)

        args.insert(0, func)
        val = tuple(args)
        # end from make_blockwise_graph

        return val

    return blockwise_fn


def make_blockwise_function_flattened(
    func: Callable[..., Any],
    output: str,
    out_indices: Sequence[Union[str, int]],
    *arrind_pairs: Any,
    numblocks: Optional[Dict[str, Tuple[int, ...]]] = None,
    new_axes: Optional[Dict[int, int]] = None,
) -> Callable[[List[int]], Any]:
    # TODO: make this a part of make_blockwise_function?
    blockwise_fn = make_blockwise_function(
        func, output, out_indices, *arrind_pairs, numblocks=numblocks, new_axes=new_axes
    )

    def blockwise_fn_flattened(out_key):
        name_chunk_inds = blockwise_fn(out_key)[1:]  # drop function in position 0
        # flatten (nested) lists indicating contraction
        if isinstance(name_chunk_inds[0], list):
            name_chunk_inds = list(flatten(name_chunk_inds))
        return name_chunk_inds

    return blockwise_fn_flattened
