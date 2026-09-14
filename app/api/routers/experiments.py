from fastapi import APIRouter, HTTPException, Query

from app.experiments.service import ExperimentDataError, ExperimentReadService


def build_experiments_router(service: ExperimentReadService) -> APIRouter:
    router = APIRouter(prefix="/api/experiments", tags=["experiments"])

    @router.get("")
    def list_experiments():
        return service.list_experiments()

    def read(operation):
        try:
            return operation()
        except KeyError as exc:
            raise HTTPException(404, "实验或题目不存在。") from exc
        except (ExperimentDataError, TypeError, AttributeError) as exc:
            raise HTTPException(409, "实验记录无法读取，请检查归档内容和冻结版本。") from exc

    @router.get("/semantic-observation")
    def semantic_observation():
        return read(service.semantic_observation)

    @router.get("/{experiment_id}")
    def experiment(experiment_id: str):
        return read(lambda: service.describe(experiment_id))

    @router.get("/{experiment_id}/tasks/{task_id}")
    def task(
        experiment_id: str,
        task_id: str,
        path: str = Query(default="project_run", pattern="^(project_run|quick_report)$"),
    ):
        return read(lambda: service.task_detail(experiment_id, task_id, path))

    return router
