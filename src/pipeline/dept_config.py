"""
본부(department) 설정 로더
config/departments.yaml 을 읽어 본부별 설정을 반환합니다.
"""

import os
from pathlib import Path

try:
    import yaml
except ImportError:
    raise SystemExit("pyyaml이 필요합니다: pip install pyyaml") from None

CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "departments.yaml"


def load_dept(dept_name: str) -> dict:
    """
    본부 이름으로 설정을 로드합니다.
    반환값에 notion_token 이 실제 값으로 채워집니다.

    Args:
        dept_name: departments.yaml의 key (예: "strategic")

    Returns:
        {
            name, notion_token, qdrant_collection, falkordb_graph,
            mcp_port, data_dir (Path), description
        }
    """
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"설정 파일 없음: {CONFIG_PATH}")

    with CONFIG_PATH.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)

    departments = config.get("departments", {})
    if dept_name not in departments:
        available = ", ".join(departments.keys())
        raise ValueError(f"본부 '{dept_name}'를 찾을 수 없습니다. 사용 가능: {available}")

    dept = departments[dept_name].copy()

    # 환경변수에서 실제 Notion 토큰 로드
    token_env = dept.get("notion_token_env", "NOTION_TOKEN")
    notion_token = os.environ.get(token_env, "")
    if not notion_token:
        raise ValueError(
            f"Notion 토큰이 없습니다. .env 파일에 {token_env}=ntn_xxx... 를 추가하세요."
        )

    dept["notion_token"] = notion_token
    dept["data_dir"] = Path(__file__).parent.parent.parent / dept["data_dir"]

    return dept


def list_depts() -> list[str]:
    """사용 가능한 본부 목록 반환"""
    if not CONFIG_PATH.exists():
        return []
    with CONFIG_PATH.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return list(config.get("departments", {}).keys())
