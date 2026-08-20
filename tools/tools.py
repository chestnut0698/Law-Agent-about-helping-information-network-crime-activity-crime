from tools.get_time import *
from tools.web import *
from tools.search_lawlibrary import *
from tools.policy_query import *
from tools.files import *



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
}
