from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Type

from langchain_core.callbacks.manager import AsyncCallbackManagerForToolRun, CallbackManagerForToolRun
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, PrivateAttr

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "data" / "uploads"


class ListContractsInput(BaseModel):
    """No args: list raw uploaded files under backend/data/uploads."""

    pass


class ListContractsTool(BaseTool):
    name: str = "list_contracts"
    description: str = (
        "DEBUG ONLY. List raw files from backend/data/uploads. "
        "This is not the source of truth for ingested contracts and may contain duplicates. "
        "For business contract list, use list_documents."
    )
    args_schema: Type[BaseModel] = ListContractsInput
    model_config = ConfigDict(arbitrary_types_allowed=True)
    _root_dir: Path = PrivateAttr()

    def __init__(self, root_dir: Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self._root_dir = root_dir.resolve()

    def _run(
        self,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(UPLOAD_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)

        if not files:
            return "[DEBUG uploads] No files found in backend/data/uploads."

        lines = [
            "[DEBUG uploads] Raw uploaded files (may include duplicates):",
            "Use list_documents for database-backed contract list.",
            "",
        ]
        idx = 1
        for fpath in files:
            if not fpath.is_file():
                continue
            stat = fpath.stat()
            size_kb = stat.st_size / 1024
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            display_name = fpath.name.split("_", 1)[-1] if "_" in fpath.name else fpath.name

            lines.append(f"{idx}. {display_name} ({size_kb:.1f} KB, uploaded_at={mtime} UTC)")
            lines.append(f"   path: {fpath.resolve()}")
            idx += 1

        return "\n".join(lines)

    async def _arun(
        self,
        run_manager: AsyncCallbackManagerForToolRun | None = None,
    ) -> str:
        return await asyncio.to_thread(self._run, None)
