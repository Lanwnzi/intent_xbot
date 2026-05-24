from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Type

from langchain_core.callbacks.manager import AsyncCallbackManagerForToolRun, CallbackManagerForToolRun
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "data" / "uploads"


class ListContractsInput(BaseModel):
    """无需参数，列出所有已上传的合同文件。"""

    pass


class ListContractsTool(BaseTool):
    name: str = "list_contracts"
    description: str = (
        "列出所有已上传到库中的合同文件。"
        "当用户要求审核合同但未指定具体文件时，先调用此工具查看可用合同列表。"
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
            return "当前库中没有任何合同文件。请先上传文件。"

        lines = ["当前库中的合同文件：", ""]
        for idx, fpath in enumerate(files, start=1):
            if not fpath.is_file():
                continue
            stat = fpath.stat()
            size_kb = stat.st_size / 1024
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            # 去掉 uuid 前缀，显示原始文件名
            display_name = fpath.name
            if "_" in display_name:
                display_name = display_name.split("_", 1)[-1]

            lines.append(f"{idx}. {display_name} ({size_kb:.1f} KB, 上传于 {mtime})")
            lines.append(f"   路径: {fpath.resolve()}")

        return "\n".join(lines)

    async def _arun(
        self,
        run_manager: AsyncCallbackManagerForToolRun | None = None,
    ) -> str:
        return await asyncio.to_thread(self._run, None)
