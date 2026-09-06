from tools.get_time import *
from tools.web import *
from tools.search_lawlibrary import *
from tools.policy_query import *
from tools.materials import *
from tools.run_tasks import *
from tools.entity_review import (
    list_entity_candidates,
    get_entity_candidate_context,
    compare_candidate_fields,
    build_candidate_field_table,
    search_candidate_evidence,
    list_candidate_relations,
    validate_candidate_evidence,
    propose_entity_review,
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
    "get_task_overview": get_task_overview,
    "confirm_task_plan": confirm_task_plan,
    "refresh_task_materials": refresh_task_materials,
    "delete_task_material": delete_task_material,
    "run_task_collision": run_task_collision,
    "run_task_timeline": run_task_timeline,
    "write_ai_clues": write_ai_clues,
    "read_artifact": read_artifact,
    "list_entity_candidates": list_entity_candidates,
    "get_entity_candidate_context": get_entity_candidate_context,
    "compare_candidate_fields": compare_candidate_fields,
    "build_candidate_field_table": build_candidate_field_table,
    "search_candidate_evidence": search_candidate_evidence,
    "list_candidate_relations": list_candidate_relations,
    "validate_candidate_evidence": validate_candidate_evidence,
    "propose_entity_review": propose_entity_review,
}
