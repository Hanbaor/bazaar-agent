# 本模块属于客户端层，供 Agent 工作进程和 MCP 适配层通过 HTTP 调用市场。
# 它不直接读写数据库；传输失败时先查证，重发仍沿用原动作号。
import httpx


# 类说明：持有任务身份的 HTTP 客户端，统一处理请求、原动作查证和有限重发。
class MarketClient:
    # 用途：保存市场服务地址与任务身份，创建 HTTP 客户端；测试可注入自己的传输客户端。
    def __init__(self, base_url: str, task_id: str, token: str, transport_client=None) -> None:
        self.task_id = task_id
        self.token = token
        self.owns_client = transport_client is None
        self.http = transport_client or httpx.Client(base_url=base_url.rstrip("/"), timeout=20)

    # 用途：只关闭本对象自己创建的 HTTP 客户端，避免误关测试共享连接。
    def close(self) -> None:
        if self.owns_client:
            self.http.close()

    # 用途：统一发送带任务 Bearer 令牌的 HTTP 请求，业务参数放在 JSON 请求体中。
    def _request(self, method: str, path: str, body=None):
        return self.http.request(
            method, path, headers={"Authorization": f"Bearer {self.token}"}, json=body
        )

    # 用途：获取并解析当前任务的公开状态，HTTP 失败时抛出异常。
    def snapshot(self) -> dict:
        response = self._request("GET", f"/tasks/{self.task_id}")
        response.raise_for_status()
        return response.json()

    # 用途：把一条模型调用记录提交到服务端 llm_calls 接口，失败时保留异常供上层处理。
    def record_call(self, record: dict) -> None:
        response = self._request("POST", f"/tasks/{self.task_id}/llm-calls", record)
        response.raise_for_status()

    # 用途：查证原动作编号的已提交结果；未查到或查询失败时返回 None，不推断原操作失败。
    def operation(self, action_id: str) -> dict | None:
        try:
            response = self._request("GET", f"/tasks/{self.task_id}/operations/{action_id}")
            response.raise_for_status()
            data = response.json()["data"]
            return data["result"] if data["known"] else None
        except (httpx.HTTPError, ValueError, KeyError):
            return None

    # 用途：用同一动作号发送请求，传输异常后先查原结果，再有限重发。
    # 仍无法查证时返回 unknown，要求执行器保留原操作号并停止新采购。
    def execute(self, action_id: str, name: str, arguments: dict) -> dict:
        body = {"action_id": action_id, "tool": name, "arguments": arguments}
        for _ in range(3):
            try:
                response = self._request("POST", f"/tasks/{self.task_id}/actions", body)
                if response.status_code in {401, 403, 404, 422}:
                    code = "INVALID_ARGUMENT" if response.status_code == 422 else "NOT_FOUND"
                    return {
                        "status": "rejected",
                        "data": None,
                        "error": {"code": code, "message": "请求被接口拒绝"},
                    }
                response.raise_for_status()
                result = response.json()
                if result.get("status") in {"success", "rejected", "approval_required"}:
                    return result
            except (httpx.HTTPError, ValueError):
                # 请求可能已经提交；绝不能据此分配新动作号再次买一件。
                pass
            resolved = self.operation(action_id)
            if resolved is not None:
                return resolved
        return {
            "status": "unknown",
            "data": {"action_id": action_id},
            "error": {
                "code": "RESULT_UNKNOWN",
                "message": "暂未查证结果，保留原操作号并停止新采购",
            },
        }
