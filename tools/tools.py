from tools.get_time import *
from tools.web import *
from tools.search_lawlibrary import *
from tools.policy_query import *
from tools.files import *
from tools.files_for_workspace import *
from tools.tasks import (
    confirm_task_plan,
    generate_task_clues,
    get_task_overview,
    refresh_task_materials,
    run_task_collision,
    run_task_timeline,
)


# 会话附件兼容路径：未经卷宗脱敏门控，不得进入抽取/碰撞上下文。
WORKSPACE_ONLY_TOOLS = {"list_user_files", "read_any_file"}

tool_functions = {
    "get_current_time": get_current_time,
    "web_search": web_search,
    "web_fetch": web_fetch,
    "search_lawlibrary": search_lawlibrary,
    "search_policy": search_policy,
    "list_case_materials": list_case_materials,
    "get_material_status": get_material_status,
    "locate_low_quality_pages": locate_low_quality_pages,
    "read_material_chunk": read_material_chunk,
    "submit_ocr_correction": submit_ocr_correction,
    "list_user_files": list_user_files,
    "read_any_file": read_any_file,
    "get_task_overview": get_task_overview,
    "confirm_task_plan": confirm_task_plan,
    "refresh_task_materials": refresh_task_materials,
    "run_task_collision": run_task_collision,
    "run_task_timeline": run_task_timeline,
    "generate_task_clues": generate_task_clues,
}
