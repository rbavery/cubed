import networkx as nx
from tenacity import retry, stop_after_attempt

from cubed.runtime.types import DagExecutor


@retry(stop=stop_after_attempt(3))
def exec_stage_func(func, *args, **kwargs):
    return func(*args, **kwargs)


class PythonDagExecutor(DagExecutor):
    @staticmethod
    def execute_dag(dag, task_callback=None, **kwargs):
        dag = dag.copy()
        for node in reversed(list(nx.topological_sort(dag))):
            pipeline = nx.get_node_attributes(dag, "pipeline").get(node, None)
            if pipeline is None:
                continue

            for stage in pipeline.stages:
                if stage.mappable is not None:
                    for m in stage.mappable:
                        exec_stage_func(stage.function, m, config=pipeline.config)
                        if task_callback is not None:
                            task_callback.increment()
                else:
                    exec_stage_func(stage.function, config=pipeline.config)
                    if task_callback is not None:
                        task_callback.increment()
