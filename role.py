import json
import re
from typing import Dict, Any, List, Optional


async def execute(data: dict, ctx) -> dict:
    text = data.get('text', '').strip()
    if not text:
        return _blocked('输入为空')

    job_id = _extract_df_id(text)
    if not job_id:
        return _blocked('未在输入中找到有效的DF-编号')

    # Call both required tools
    try:
        job_res = await ctx.tools.call("devflow.job.read", jobId=job_id)
    except Exception:
        return _blocked(f"devflow.job.read 调用失败: {job_id}")

    try:
        catalog_res = await ctx.tools.call("devflow.catalog.search", query="")
    except Exception:
        return _blocked(f"devflow.catalog.search 调用失败")

    # Validate non-empty results
    if not isinstance(job_res, list) or len(job_res) == 0:
        return _blocked(f"devflow.job.read 返回空或无效结果: {job_res!r}")
    if not isinstance(catalog_res, list):
        return _blocked(f"devflow.catalog.search 返回非列表: {catalog_res!r}")

    # Build evidence for LLM
    evidence = {
        "job_record": job_res[0],
        "catalog_records": catalog_res
    }

    # Compile prompt with static role rules and dynamic evidence
    prompt = await ctx.prompt('role')
    system_msg = prompt.compile()
    user_msg = json.dumps({"input": text, "evidence": evidence}, ensure_ascii=False)

    # LLM decision only
    try:
        llm_out = await ctx.llm.chat([
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg}
        ], prompt=prompt)
        decision_obj = json.loads(llm_out.strip())
        decision = decision_obj.get('decision')
        if decision not in ['PASS', 'BLOCKED']:
            return _blocked(f'LLM 输出 decision 字段值非法: {decision!r}')
    except (json.JSONDecodeError, ValueError, TypeError):
        return _blocked('LLM 输出不是合法 JSON 或缺少 decision 字段')

    if decision == 'BLOCKED':
        block_reason = decision_obj.get('block_reason', '模型判定阻塞')
        return {
            "decision": "BLOCKED",
            "block_reason": block_reason,
            "evidence_sources": ["devflow.job.read", "devflow.catalog.search"],
            "steps": [
                "提取DF-编号",
                "调用devflow.job.read获取任务记录",
                "调用devflow.catalog.search获取工具目录",
                "验证工具返回非空",
                "提交证据至LLM审核",
                "LLM返回BLOCKED"
            ]
        }

    # PASS path: assemble human-readable answer from real evidence
    job = job_res[0]
    status = job.get('status', '').upper()
    progress_desc = {
        'RUN': '正在运行中',
        'WAIT': '等待依赖完成',
        'HUMAN': '需人工介入',
        'QUEUED': '已在队列中等待执行',
        'DONE': '已成功完成',
        'FAIL': '执行失败',
        'CANCEL': '已被取消'
    }.get(status, f'状态未知：{status}')

    tools_list = [
        t.get('name', '未知工具')
        for t in catalog_res
        if t.get('access') == 'read' and t.get('risk') == 'low'
    ]
    tools_desc = "、".join(tools_list) if tools_list else "暂无可用只读工具"

    answer = f"任务 {job_id} 当前状态：{progress_desc}。可用只读工具：{tools_desc}。"

    return {
        "decision": "PASS",
        "answer": answer,
        "evidence_sources": ["devflow.job.read", "devflow.catalog.search"],
        "steps": [
            "提取DF-编号",
            "调用devflow.job.read获取任务记录",
            "调用devflow.catalog.search获取工具目录",
            "验证工具返回非空",
            "提交证据至LLM审核",
            "LLM返回PASS，外层组装中文进度说明与工具建议"
        ]
    }


def _extract_df_id(text: str) -> Optional[str]:
    # Match DF- followed by 4+ digits, tolerant of Chinese/ASCII punctuation and whitespace.
    # Must match *exactly one* DF-<digits> in input; multiple IDs → BLOCKED per H2 boundary case a-4.
    # Supports: "DF-0010", "任务DF-0010", "DF-0010、DF-0011" (first only), "DF-0010，给出建议", "DF-0010。"
    # Uses simple left-to-right greedy match — no lookbehind/lookahead needed for this spec.
    pattern = r'DF-(\d{4,})'
    matches = re.findall(pattern, text)
    if len(matches) != 1:
        return None
    return f"DF-{matches[0]}"


def _blocked(reason: str) -> dict:
    return {
        "decision": "BLOCKED",
        "block_reason": reason,
        "evidence_sources": [],
        "steps": [
            "输入预检失败",
            f"阻塞原因：{reason}"
        ]
    }