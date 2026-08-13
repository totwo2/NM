"""
服务启动与数据兼容测试
覆盖: web.server 可导入、main_entry 存在、OA 旧数据反序列化容错
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nm.oa.oa_store import OAStore
from nm.oa.workflow import WorkflowEngine


class TestServerBoot:
    def test_web_server_imports(self):
        import web.server
        assert hasattr(web.server, "app")
        assert hasattr(web.server, "main_entry")

    def test_has_auth_middleware(self):
        import web.server
        assert hasattr(web.server, "auth_middleware")


class TestOACompat:
    def test_legacy_step_record_extra_field(self):
        """旧数据含 operator 字段，不应导致反序列化崩溃"""
        path = tempfile.mktemp(suffix=".json")
        with open(path, "w") as f:
            import json
            json.dump({
                "defs": [],
                "instances": [{
                    "id": "OA-20260101-0001",
                    "def_id": "leave_approval",
                    "title": "测试",
                    "initiator": "zhangsan",
                    "initiator_name": "张三",
                    "form_data": {"days": 2},
                    "status": "pending",
                    "current_step": 0,
                    "history": [{
                        "step_id": "step_1",
                        "step_name": "直属领导审批",
                        "assignee": "lisi",
                        "action": "delegate",
                        "operator": "lisi",      # 旧字段，应忽略
                        "operator_name": "李四",  # 旧字段，应忽略
                    }],
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }],
                "seq": 5,
            }, f)
        store = OAStore(path)
        insts = store.list_instances()
        assert len(insts) == 1
        assert len(insts[0].history) == 1
        assert insts[0].history[0].action == "delegate"
        os.remove(path)

    def test_legacy_missing_step_fields(self):
        """旧数据 StepRecord 缺必填字段，应补默认值而非崩溃"""
        path = tempfile.mktemp(suffix=".json")
        with open(path, "w") as f:
            import json
            json.dump({
                "defs": [],
                "instances": [{
                    "id": "OA-20260101-0002",
                    "def_id": "x",
                    "title": "t",
                    "initiator": "u",
                    "status": "completed",
                    "current_step": 1,
                    "history": [{"step_id": "s1", "assignee": "a"}],  # 缺 step_name/action/comment
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }],
                "seq": 6,
            }, f)
        store = OAStore(path)
        insts = store.list_instances()
        assert len(insts) == 1
        assert insts[0].history[0].step_name == "s1"  # fallback 到 step_id
        os.remove(path)

    def test_delete_def_dedup(self):
        """删除模板接口正常（修复重复定义后）"""
        path = tempfile.mktemp(suffix=".json")
        store = OAStore(path)
        engine = WorkflowEngine(store)
        engine.register_def(
            id="t1", name="测试模板", steps=[{"name": "审批"}]
        )
        assert engine.get_def("t1") is not None
        assert engine.delete_def("t1") is True
        assert engine.get_def("t1") is None
        os.remove(path)