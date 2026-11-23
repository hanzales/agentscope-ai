"""Small PostgreSQL/SQLite maintenance worker; no external queue is required."""

import argparse
import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select

from app.models import Project, Trace
from app.storage.database import Session, engine

log = structlog.get_logger()


async def run_once() -> None:
    async with Session() as session:
        projects = (await session.scalars(select(Project))).all()
        for project in projects:
            cutoff = datetime.now(UTC) - timedelta(days=project.retention_days)
            expired = (
                await session.scalars(
                    select(Trace).where(
                        Trace.project_id == project.id,
                        Trace.started_at < cutoff,
                    )
                )
            ).all()
            for trace in expired:
                await session.delete(trace)
            log.info(
                "retention_complete",
                project_id=project.id,
                retention_days=project.retention_days,
                deleted_traces=len(expired),
            )
        await session.commit()


async def main(interval: int, once: bool) -> None:
    try:
        while True:
            await run_once()
            if once:
                return
            await asyncio.sleep(interval)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run TAMGA AI maintenance jobs")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=3600)
    args = parser.parse_args()
    asyncio.run(main(max(args.interval, 60), args.once))
