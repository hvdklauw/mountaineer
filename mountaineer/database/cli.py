from types import ModuleType
from typing import overload

from click import secho
from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from mountaineer.database.dependencies import DatabaseDependencies
from mountaineer.database.sqlmodel import SQLModel
from mountaineer.dependencies import get_function_dependencies


@overload
async def handle_createdb(model_module: ModuleType) -> None:
    ...


@overload
async def handle_createdb(models: list[SQLModel]) -> None:
    ...


async def handle_createdb(*args, **kwargs):
    """
    Strictly speaking, passing a list of models isn't required. We just encourage
    an explicit passing of either the models module or the SQLModels themselves to make
    sure they are in-scope of the table registry when this function is run.

    """

    async def run_migrations(
        engine: AsyncEngine = Depends(DatabaseDependencies.get_db),
    ):
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)

            # Log the tables that were created
            result = await connection.execute(
                text(
                    """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                """
                )
            )
            tables = "\n".join([f"* {table[0]}" for table in result.fetchall()])
            secho(f"Database tables created:\n{tables}", fg="green")

    async with get_function_dependencies(callable=run_migrations) as values:
        await run_migrations(**values)
