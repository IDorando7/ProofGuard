from fastapi import BackgroundTasks

from app.workers.audit_jobs import prepare_audit_project


def start_prepare_job(background_tasks: BackgroundTasks, project_id: str) -> None:
    background_tasks.add_task(prepare_audit_project, project_id)

