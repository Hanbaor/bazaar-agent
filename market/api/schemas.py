# 本模块属于 HTTP 接口层，声明客户端可以提交的外层请求数据。
# FastAPI 先校验这些模型，Service 再用 market.tools 校验具体工具参数。
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from market.tools import FinishParams


# 类说明：HTTP 请求模型的共同基类，禁止未声明字段。
class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# 类说明：只允许选择已提供的三个开发场景，不能传任意本地文件路径。
class CreateTask(RequestModel):
    scenario: Literal["s01_basic", "s02_stockout", "s03_low_budget"] = "s01_basic"


# 类说明：执行器提交的一次工具调用，外层带动作号，内层是业务参数。
class ActionRequest(RequestModel):
    action_id: str = Field(min_length=1, max_length=120)
    tool: str = Field(min_length=1)
    arguments: dict = Field(default_factory=dict)


# 类说明：网页人工断货请求，由 owner 身份调用，不开放给普通 Agent 工具。
class InterventionRequest(RequestModel):
    action_id: str = Field(min_length=1, max_length=120)
    listing_id: str = Field(min_length=1)


# 类说明：结束报告的 HTTP 外层结构，把动作号与报告内容分开。
class ReportRequest(RequestModel):
    action_id: str = Field(min_length=1, max_length=120)
    report: FinishParams


# 类说明：一次模型调用的观测数据；费用缺少依据时允许为未知值。
class LLMCallRequest(RequestModel):
    call_id: str = Field(min_length=1)
    model: str
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    duration_ms: int = Field(ge=0)
    estimated_cost_yuan: str | None = None
    error_code: str | None = None
