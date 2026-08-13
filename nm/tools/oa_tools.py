"""
OA 工具: 基于模板的 Word/Excel/PPT 内容填充
不生成格式 — 格式在模板中由人在 WPS/Office 中预设
python-docx/openpyxl/python-pptx 只替换占位符文本
"""

import os
import json
from typing import Any


def create_oa_tools(workspace_dir: str) -> list[dict[str, Any]]:
    """创建 OA 工具定义列表"""

    # ========================================================================
    # fill_docx_template
    # ========================================================================
    def _fill_docx_template(
        template_path: str,
        output_path: str,
        placeholders: str,  # JSON 字符串: {"{title}": "关于...", "{body}": "内容..."}
    ) -> str:
        """填充 Word 模板中的占位符"""
        from docx import Document
        from docx.shared import Pt, RGBColor

        full_template = _resolve_oa_path(workspace_dir, template_path)
        full_output = _resolve_oa_path(workspace_dir, output_path)

        if not os.path.exists(full_template):
            return f"错误: 模板文件不存在: {template_path}"

        try:
            data = json.loads(placeholders)
        except json.JSONDecodeError:
            return f"错误: placeholders 不是有效的 JSON。格式: {{\"{{title}}\": \"新标题\", \"{{body}}\": \"新内容\"}}"

        try:
            doc = Document(full_template)

            # 替换所有段落中的占位符
            replaced_count = 0
            for para in doc.paragraphs:
                for run in para.runs:
                    for placeholder, value in data.items():
                        if placeholder in run.text:
                            run.text = run.text.replace(placeholder, str(value))
                            replaced_count += 1
                # 兜底: 段落级整体替换（处理占位符跨 run 的情况，如 {tit}+{le}）
                full = para.text
                for placeholder, value in data.items():
                    if placeholder in full:
                        # 找到含占位符的 run，整体替换
                        for run in para.runs:
                            run.text = run.text.replace(placeholder, str(value))
                        replaced_count += 1

            # 替换表格中的占位符
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        for para in cell.paragraphs:
                            for run in para.runs:
                                for placeholder, value in data.items():
                                    if placeholder in run.text:
                                        run.text = run.text.replace(placeholder, str(value))
                                        replaced_count += 1
                            # 兜底: 段落级整体替换
                            full = para.text
                            for placeholder, value in data.items():
                                if placeholder in full:
                                    for run in para.runs:
                                        run.text = run.text.replace(placeholder, str(value))
                                    replaced_count += 1

            os.makedirs(os.path.dirname(full_output) or ".", exist_ok=True)
            doc.save(full_output)

            return (
                f"Word 模板填充完成: {output_path}\n"
                f"模板: {template_path}\n"
                f"替换了 {replaced_count} 处占位符\n"
                f"填充字段: {list(data.keys())}"
            )

        except Exception as e:
            return f"Word 模板填充失败: {e}"

    # ========================================================================
    # fill_xlsx_template
    # ========================================================================
    def _fill_xlsx_template(
        template_path: str,
        output_path: str,
        sheet_data: str,  # JSON: {"Sheet1": [["列1","列2"], ["值1","值2"]], "Sheet2": ...}
    ) -> str:
        """填充 Excel 模板的数据"""
        from openpyxl import load_workbook

        full_template = _resolve_oa_path(workspace_dir, template_path)
        full_output = _resolve_oa_path(workspace_dir, output_path)

        if not os.path.exists(full_template):
            return f"错误: 模板文件不存在: {template_path}"

        try:
            data = json.loads(sheet_data)
        except json.JSONDecodeError:
            return f"错误: sheet_data 不是有效的 JSON"

        try:
            wb = load_workbook(full_template)

            for sheet_name, rows in data.items():
                if sheet_name not in wb.sheetnames:
                    return f"错误: 模板中不存在 Sheet '{sheet_name}'。可用 Sheet: {wb.sheetnames}"

                ws = wb[sheet_name]
                for r, row_data in enumerate(rows, start=1):
                    for c, value in enumerate(row_data, start=1):
                        cell = ws.cell(row=r, column=c)
                        # 不覆盖已有公式
                        if not str(cell.value).startswith("="):
                            cell.value = value

            os.makedirs(os.path.dirname(full_output) or ".", exist_ok=True)
            wb.save(full_output)

            sheet_names = ", ".join(data.keys())
            return f"Excel 模板填充完成: {output_path}\n模板: {template_path}\n填充 Sheet: {sheet_names}"

        except Exception as e:
            return f"Excel 模板填充失败: {e}"

    # ========================================================================
    # fill_pptx_template
    # ========================================================================
    def _fill_pptx_template(
        template_path: str,
        output_path: str,
        slide_data: str,  # JSON: {"slide_placeholders": {"{title}": "...", "{subtitle}": "..."}}
    ) -> str:
        """填充 PPT 模板的占位符"""
        from pptx import Presentation

        full_template = _resolve_oa_path(workspace_dir, template_path)
        full_output = _resolve_oa_path(workspace_dir, output_path)

        if not os.path.exists(full_template):
            return f"错误: 模板文件不存在: {template_path}"

        try:
            data = json.loads(slide_data)
        except json.JSONDecodeError:
            return f"错误: slide_data 不是有效的 JSON"

        try:
            prs = Presentation(full_template)
            replaced_count = 0

            for slide in prs.slides:
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        for para in shape.text_frame.paragraphs:
                            for run in para.runs:
                                for placeholder, value in data.items():
                                    if placeholder in run.text:
                                        run.text = run.text.replace(placeholder, str(value))
                                        replaced_count += 1

            os.makedirs(os.path.dirname(full_output) or ".", exist_ok=True)
            prs.save(full_output)

            return (
                f"PPT 模板填充完成: {output_path}\n"
                f"模板: {template_path}\n"
                f"替换了 {replaced_count} 处占位符\n"
                f"填充字段: {list(data.keys())}"
            )

        except Exception as e:
            return f"PPT 模板填充失败: {e}"

    # ========================================================================
    # read_docx (OA 读取工具)
    # ========================================================================
    def _read_docx(path: str) -> str:
        """读取 Word 文档内容"""
        from docx import Document

        full_path = _resolve_oa_path(workspace_dir, path)
        if not os.path.exists(full_path):
            return f"文件不存在: {path}"

        try:
            doc = Document(full_path)
            paragraphs = []
            for para in doc.paragraphs:
                if para.text.strip():
                    paragraphs.append(para.text)

            # 读取表格
            tables_text = []
            for i, table in enumerate(doc.tables):
                rows = []
                for row in table.rows:
                    cells = [cell.text for cell in row.cells]
                    rows.append(" | ".join(cells))
                tables_text.append(f"\n[表格 {i+1}]\n" + "\n".join(rows))

            result = "\n".join(paragraphs)
            if tables_text:
                result += "\n" + "\n".join(tables_text)

            return f"[Word 文档: {path}]\n{result}"

        except Exception as e:
            return f"读取 Word 失败: {e}"

    # ========================================================================
    # read_xlsx (OA 读取工具)
    # ========================================================================
    def _read_xlsx(path: str, sheet: str = "", max_rows: int = 100) -> str:
        """读取 Excel 文档内容"""
        from openpyxl import load_workbook

        full_path = _resolve_oa_path(workspace_dir, path)
        if not os.path.exists(full_path):
            return f"文件不存在: {path}"

        try:
            wb = load_workbook(full_path, data_only=True)

            # 列出所有 Sheet
            if not sheet:
                sheet = wb.sheetnames[0]
                info = f"[Excel: {path}] [Sheet 列表: {wb.sheetnames}] [当前: {sheet}]\n"
            else:
                if sheet not in wb.sheetnames:
                    return f"Sheet '{sheet}' 不存在。可用: {wb.sheetnames}"
                info = f"[Excel: {path}] [Sheet: {sheet}]\n"

            ws = wb[sheet]
            rows = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= max_rows:
                    rows.append(f"...(共 {ws.max_row} 行，仅显示前 {max_rows} 行)")
                    break
                # 过滤全空行
                if any(v is not None for v in row):
                    rows.append(" | ".join(str(v) if v is not None else "" for v in row))

            return info + "\n".join(rows)

        except Exception as e:
            return f"读取 Excel 失败: {e}"

    return [
        {
            "name": "fill_docx_template",
            "description": (
                "填充 Word 模板。模板需提前在 WPS/Word 中制作好，用 {{字段名}} 标记占位符。"
                "此工具只替换文本，不改格式（字体、颜色、页边距等）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "template_path": {"type": "string", "description": "模板文件路径"},
                    "output_path": {"type": "string", "description": "输出文件路径"},
                    "placeholders": {
                        "type": "string",
                        "description": '占位符映射，JSON 格式。例: {"{{title}}": "关于调整考勤制度的通知", "{{body}}": "正文内容..."}',
                    },
                },
                "required": ["template_path", "output_path", "placeholders"],
            },
            "fn": _fill_docx_template,
            "requires_approval": False,
            "timeout_seconds": 30,
        },
        {
            "name": "fill_xlsx_template",
            "description": (
                "填充 Excel 模板。模板需提前在 WPS/Excel 中制作好（含表头、公式、格式）。"
                "此工具只填充数据行，不改格式和公式。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "template_path": {"type": "string", "description": "模板文件路径"},
                    "output_path": {"type": "string", "description": "输出文件路径"},
                    "sheet_data": {
                        "type": "string",
                        "description": 'Sheet 数据映射，JSON 格式。例: {"Sheet1": [["姓名","部门"],["张三","财务部"]]}',
                    },
                },
                "required": ["template_path", "output_path", "sheet_data"],
            },
            "fn": _fill_xlsx_template,
            "requires_approval": False,
            "timeout_seconds": 30,
        },
        {
            "name": "fill_pptx_template",
            "description": "填充 PPT 模板中的文本占位符。",
            "parameters": {
                "type": "object",
                "properties": {
                    "template_path": {"type": "string", "description": "模板文件路径"},
                    "output_path": {"type": "string", "description": "输出文件路径"},
                    "slide_data": {
                        "type": "string",
                        "description": '占位符映射，JSON 格式。例: {"{title}": "2026 年度总结", "{subtitle}": "财务部"}',
                    },
                },
                "required": ["template_path", "output_path", "slide_data"],
            },
            "fn": _fill_pptx_template,
            "requires_approval": False,
            "timeout_seconds": 30,
        },
        {
            "name": "read_docx",
            "description": "读取 Word (.docx) 文档的文本内容和表格数据。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Word 文件路径"},
                },
                "required": ["path"],
            },
            "fn": _read_docx,
            "requires_approval": False,
            "timeout_seconds": 15,
        },
        {
            "name": "read_xlsx",
            "description": "读取 Excel (.xlsx) 文档的内容。默认读取第一个 Sheet。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Excel 文件路径"},
                    "sheet": {"type": "string", "description": "Sheet 名称（默认第一个）", "default": ""},
                    "max_rows": {"type": "integer", "description": "最大读取行数", "default": 100},
                },
                "required": ["path"],
            },
            "fn": _read_xlsx,
            "requires_approval": False,
            "timeout_seconds": 15,
        },
    ]


def _resolve_oa_path(workspace_dir: str, path: str) -> str:
    """解析 OA 文件路径"""
    if os.path.isabs(path):
        return path
    return os.path.join(workspace_dir, path)
