"""Public diagnostics from HTTP status and allowlisted provider codes only.

An error category is not permission to repeat a potentially billable request.
Never expose provider messages, headers, request bodies, or unknown error codes.
"""

# Verified against https://docs.bigmodel.cn/cn/api/api-code (2026-11-04).
# Match both HTTP status and business code; other providers use other namespaces.
_ZHIPU_ERRORS = {
    (401, "1000"): "模型身份验证失败，请检查本地 API Key",
    (401, "1001"): "模型请求缺少身份凭据，请检查本地 API Key",
    (401, "1003"): "模型身份凭据已过期，请检查本地 API Key",
    (401, "1005"): "账户要求二次认证，请在供应商控制台检查认证设置",
    (429, "1113"): "供应商报告账户欠费，请核对控制台余额及资源包适用范围",
    (400, "1211"): "模型不存在，请检查模型配置",
    (403, "1220"): "账户没有该模型或接口的访问权限，请在控制台核对",
    (400, "1261"): "模型输入超过长度限制，请缩短消息或物件描述",
    (400, "1301"): "输入或生成内容被供应商审核拒绝，请调整描述",
    (429, "1302"): "账户请求频率达到限制，请稍后再发起请求",
    (429, "1305"): "模型暂时繁忙，请稍后再发起请求",
    (429, "1308"): "账户达到使用上限，请在控制台核对额度及重置时间",
    (429, "1309"): "编程套餐已到期，请在控制台核对套餐及 API Key 类型",
    (429, "1310"): "套餐达到每周或每月使用上限，请在控制台核对重置时间",
    (429, "1311"): "当前套餐未开放该模型权限，请核对模型和 API Key 类型",
    (429, "1313"): "账户受到供应商使用策略限制，请在控制台检查",
    (429, "1314"): "企业套餐已失效，请联系账户管理员",
    (429, "1315"): "API Key 的产品类型不匹配，请在控制台核对",
}
for _code in ("1316", "1317", "1318", "1319", "1320", "1321"):
    _ZHIPU_ERRORS[429, _code] = "套餐或消费额度达到上限，请在控制台核对额度及重置时间"

_HTTP_ERRORS = {
    400: "模型请求参数或内容被拒绝，请检查模型配置和描述",
    401: "模型身份验证失败，请检查本地 API Key",
    403: "模型请求被拒绝，请在控制台检查权限或服务策略",
    404: "模型或接口不可用，请检查模型配置",
    408: "模型服务响应超时，结果未确认",
    429: "模型服务限制了请求，仅凭 HTTP 429 无法判断余额，请在控制台核对",
}


def http_failure_message(response, *, zhipu=False):
    status, code = response.status_code, None
    message = None
    if zhipu and len(response.content) <= 65536:
        try:
            body = response.json()
        except (ValueError, RecursionError):
            body = None
        error = body.get("error") if isinstance(body, dict) else None
        candidate = error.get("code") if isinstance(error, dict) else None
        if type(candidate) in (str, int):  # Reject bools, floats, lists, and objects.
            candidate = str(candidate)
            message = _ZHIPU_ERRORS.get((status, candidate))
            if message:
                code = candidate
    if message is None:
        message = _HTTP_ERRORS.get(status, "模型服务暂不可用" if 500 <= status < 600 else "模型请求未完成")
    label = f"HTTP {status}" + (f" / {code}" if code else "")
    return f"{message}（{label}）；未自动重试或切换模型"
