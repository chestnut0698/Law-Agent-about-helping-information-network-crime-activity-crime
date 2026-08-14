# 信证智析

帮信罪事实—证据要素化与量刑情节核验智能体的比赛原型。当前完成 LA-001：可登录、创建案件、进入工作台，并受服务端权限与审计保护。

本系统整理事实与材料来源，不作定罪、证明标准或最终量刑结论。

## 本地运行

```powershell
pip install -e ".[dev]"
python -m alembic upgrade head
python scripts/seed_dev_users.py
python -m streamlit run src/law_agent/app/main.py
```

开发用户：`prosecutor`、`assistant`、`support`、`business_admin`、`auditor`、`system_admin`、`nonmember`  
开发密码：`DevOnly-ChangeMe!`

有创建权限的账号为 `prosecutor`。`nonmember` 用于验证非案件成员访问会被服务端拒绝。

## 验收

```powershell
python -m pytest tests/unit/test_rbac.py tests/unit/test_case_service.py -v
python -m pytest tests/e2e/test_la_001_create_and_access.py -v
python -m pytest -v
python -m ruff check src tests
```
