from fastapi import APIRouter, HTTPException, Query

from app.error_redaction import redact_error
from app.experiments.review import HumanVerdict, ReviewInputError, ReviewService
from app.experiments.service import ExperimentDataError, ExperimentReadService


def build_experiments_router(service: ExperimentReadService) -> APIRouter:
    router = APIRouter(prefix="/api/experiments", tags=["experiments"])
    review = ReviewService(service.root)

    def review_operation(operation):
        try:
            return operation()
        except ReviewInputError as exc:
            raise HTTPException(400, redact_error(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, "核验运行或题目不存在。") from exc
        except (ExperimentDataError, OSError) as exc:
            raise HTTPException(409, "核验记录无法读写，请检查评测归档和目录权限。") from exc

    @router.get("/review/runs")
    def review_runs():
        return review_operation(review.runs)

    @router.get("/review/runs/{run_ref}/items")
    def review_items(run_ref: str):
        return review_operation(lambda: review.items(run_ref))

    @router.get("/review/runs/{run_ref}/items/{key}")
    def review_item(run_ref: str, key: str):
        return review_operation(lambda: review.item(run_ref, key))

    @router.put("/review/runs/{run_ref}/items/{key}/human")
    def review_human(run_ref: str, key: str, verdict: HumanVerdict):
        return review_operation(lambda: review.save(run_ref, key, verdict))

    @router.get("/review/runs/{run_ref}/agreement")
    def review_agreement(run_ref: str):
        return review_operation(lambda: review.agreement(run_ref))

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
