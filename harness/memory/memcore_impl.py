#!/usr/bin/env python3
# 上下文连续性中间件 · 实验 v4（取经 TencentDB Agent Memory · 自研实现）
# ---------------------------------------------------------------------------
# 设计思路受 TencentDB Agent Memory (腾讯, MIT) 的分层蒸馏 / 异步批处理提取 /
# 故障降级 / 分层回灌 启发；本文件为自研 Python 实现，未复制其代码，故不留版权声明。
# 取经落地点(vs v3):
#   · 异步批处理提取: 流量侧 ingest 只入 pending 缓冲, 周期 flush() 攒批一次性提炼
#     (替代逐句 LLM 抖动+过度决策化); LLMExtractor 加 batch_extract(), Heuristic 退化为逐句。
#   · 故障降级: _handle_chat 把捕获/回灌包 try/except, 记忆系统异常时跳过记忆,
#     对话照常转发上游(故障容忍优先于功能完整)。
#   · 分层回灌降本: snapshot 分 stable(轨迹 observation 常驻)+ volatile(决策+事实全量),
#     代理拼装 system = 常驻前缀 + 全量回灌(站全量立场不漏, 稳定层可缓存友好)。
#   · ④ 冲突仲裁(两阶段): 取代旧版"同 scope 一刀切 supersede"。阶段1 本地候选召回(同 scope
#     + 关键词重合, 零模型); 阶段2 判定 add/skip/update/merge, 区分 改主意/两项目/自相矛盾。
#     无真模型时走规则兜底(确定性, 覆盖主路径); 接 LLMExtractor 后启用模型级阶段2。
# 保留 v3: 通用适配(代理零代码 + /capture /recall 契约) / 双层记忆模型 / 原始全量留存 /
#         并发写锁 / 零配置继承本机 LLM(modelroute)。
# 纯标准库, `python3 self_experiment_proxy.py`。
# ---------------------------------------------------------------------------
# 完成体分层（点→面, 2026-08-05）:
#   语义提取层 SemanticExtractor(Heuristic/LLM)  —— 对话 → 结构化记忆候选
#   仲裁层   ConflictArbiter                     —— 两阶段冲突仲裁 add/skip/update/merge
#   蒸馏层   Distiller                           —— 写路径流水线(缓冲→批提→仲裁→落库)
#   存储层   JsonStore(可插拔)                   —— 持久化后端抽象, 原子写+损坏降级
#   门面层   MemoryCore                          —— 公开 API: ingest/distill/recall/capture
#   scope 泛化: 选型(点) → user:/project:/session:/domain: 自由命名(面),
#               recall 多 scope 分组回灌, 优先级 session>project>user>domain。
# ---------------------------------------------------------------------------

import json
import os
import re
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

MEMORY_FILE = "memory_store.json"
TRANSCRIPT_FILE = "transcript.jsonl"   # 原始对话全量留存(源, 不丢)
LLM_CONFIG = "llm_config.json"
PROXY_PORT = 8787
MOCK_PORT = 8799

def _now():
    return datetime.now(timezone.utc).isoformat()

def ensure_llm_config():
    try:
        open(LLM_CONFIG, "r")
    except FileNotFoundError:
        json.dump({"base_url": f"http://127.0.0.1:{MOCK_PORT}/v1",
                    "api_key": "LOCAL_USER_KEY"},
                  open(LLM_CONFIG, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

# ============ 语义提取层 (薄弱点所在) ============
class SemanticExtractor:
    """把对话(单句或一批) → 结构化记忆候选列表。默认启发式; 真语义靠 LLM / 结构化 Hook。"""
    def extract(self, role, content, scope="选型"):
        raise NotImplementedError

class HeuristicExtractor(SemanticExtractor):
    """正则占位: 演示机制用, 非真语义。漏取/误记风险高 → 见下方 LLM 插件。
    无 batch_extract 方法 → Distiller.flush() 自动退化为逐句提取(兼容 demo)。"""
    def extract(self, role, content, scope="选型"):
        c = content
        out = []
        # ① 回退/纠正
        m = re.search(r"(回退|回到|还是用|改回|其实)\s*方案([A-Za-z0-9]+)", c)
        if m:
            choice = "方案" + m.group(2)
            pit = c if re.search(r"(崩|崩溃|不行|不兼容|出问题|失败|报错)", c) else None
            out.append({"layer": "decision", "scope": scope, "choice": choice,
                        "text": f"回退到{choice}", "verbatim": c, "pitfall_text": pit})
            return out
        # ② 决策
        m = re.search(r"(选|采用|决定用)\s*方案([A-Za-z0-9]+)", c)
        if m:
            choice = "方案" + m.group(2)
            out.append({"layer": "decision", "scope": scope, "choice": choice,
                        "text": f"当前采用{choice}", "verbatim": c})
            return out
        # ③ 耐久事实
        if re.search(r"(崩|崩溃|不行|不兼容|踩坑|失败|报错|出问题)", c):
            out.append({"layer": "fact", "kind": "pitfall", "scope": scope, "text": c, "verbatim": c})
        elif re.search(r"(可用|验证|通过|成功)", c):
            out.append({"layer": "fact", "kind": "result", "scope": scope, "text": c, "verbatim": c})
        elif re.search(r"(结论|综上|总结)", c):
            out.append({"layer": "fact", "kind": "conclusion", "scope": scope, "text": c, "verbatim": c})
        return out

def discover_qclaw_gateway():
    """零配置: 直接读用户本机 qclaw 网关(端口+token), model=openclaw(内部路由, 不指定具体模型)。
    密钥只来自 ~/.qclaw/openclaw.json, 不抄到第二处文件。
    注意: 用 'localhost' 而非 '127.0.0.1' —— 本机 iNodeClient 等可能干扰 IPv4 回环,
    localhost 解析到 IPv6(::1) 反而通(实测 127.0.0.1 超时/拒绝, localhost 正常)。"""
    p = os.path.expanduser("~/.qclaw/openclaw.json")
    try:
        d = json.load(open(p, encoding="utf-8"))
        gw = d.get("gateway", {})
        port = gw.get("port")
        token = gw.get("auth", {}).get("token")
        if port and token:
            return {"base_url": f"http://localhost:{port}/v1",
                    "api_key": token, "model": "openclaw"}
    except Exception:
        pass
    return None

def load_endpoint():
    """端点来源: 优先 llm_config.json(非本实验 mock 端口时); 否则自动发现 qclaw 网关。
    返回 (base_url,api_key,model) 或 None。
    守卫: llm_config.json 若指向本实验自带的 mock 端口(:8799)则跳过, 落回 qclaw 真端点,
    避免过期 mock 遮蔽本机真实模型。"""
    try:
        cfg = json.load(open(LLM_CONFIG, encoding="utf-8"))
        bu = cfg.get("base_url", "")
        if bu and ":8799" not in bu:
            return bu, cfg.get("api_key", ""), cfg.get("model")
    except Exception:
        pass
    q = discover_qclaw_gateway()
    if q:
        return q["base_url"], q["api_key"], q["model"]
    return None

def _chat_once(base_url, api_key, model, messages, timeout=60):
    data = {"model": model, "messages": messages, "temperature": 0}
    req = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json",
                  "Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def _robust_json(text):
    """从模型输出里抠出第一个 JSON 对象(容错: 模型可能夹带说明文字)。"""
    s = text.find("{")
    e = text.rfind("}")
    if s == -1 or e == -1 or e <= s:
        return None
    try:
        return json.loads(text[s:e+1])
    except Exception:
        return None

class LLMExtractor(SemanticExtractor):
    """真·语义提取: 调本机 OpenAI 兼容端点(modelroute, 不指定具体模型), 按 schema 抽 {决策/结论/结果/踩坑}。
    零配置: 默认自动发现 qclaw 网关(读 ~/.qclaw/openclaw.json); 或给 cfg 显式指定。
    对 429(并发)/400(偶发断流) 退避重试; 不强制 response_format(兼容 stepfun 等)。
    额外提供 batch_extract(): 攒一批对话一次性提炼(异步批处理, 根治逐句抖动+过度决策化)。"""
    SYSTEM = (
        "你是记忆提取器。阅读一句对话, 判断其中是否含值得长期记住的"
        "决策/结论/结果/踩坑点。只输出 JSON(不要任何额外说明文字): "
        '{"captures":[{"layer":"decision","scope":"选型","choice":"方案X","text":"精简","verbatim":"原句"},'
        '{"layer":"fact","kind":"pitfall|result|conclusion","scope":"选型","text":"精简","verbatim":"原句"}]}'
        "。无值得记的内容返回 {\"captures\":[]}。verbatim 必须是原句原文, 不要改写。"
    )
    SYSTEM_BATCH = (
        "你是记忆提取器。下面是一段对话片段(多句)。请从中提取所有值得长期记住的"
        "决策/结论/结果/踩坑点, 一次性输出 JSON(不要任何额外说明): "
        '{"captures":[{"layer":"decision","scope":"选型","choice":"方案X","text":"精简","verbatim":"原句"},'
        '{"layer":"fact","kind":"pitfall|result|conclusion","scope":"选型","text":"精简","verbatim":"原句"}]}'
        "。没有值得记的返回 {\"captures\":[]}。verbatim 必须是原句原文, 不要改写。"
    )
    def __init__(self, cfg=None, model=None):
        if cfg is None:
            ep = load_endpoint()
            if not ep:
                raise RuntimeError("no endpoint: 放 llm_config.json 或确保 ~/.qclaw/openclaw.json 网关可用")
            cfg = {"base_url": ep[0], "api_key": ep[1], "model": ep[2]}
        self.base_url = str(cfg["base_url"]).rstrip("/")
        self.api_key = cfg.get("api_key", "")
        self.model = model or cfg.get("model") or "openclaw"
    def _do(self, messages, max_retry=8):
        last_err = None
        for i in range(max_retry):
            try:
                j = _chat_once(self.base_url, self.api_key, self.model, messages)
                txt = j["choices"][0]["message"]["content"]
                obj = _robust_json(txt)
                if obj is None:
                    last_err = f"bad json: {txt[:80]}"
                    time.sleep(2); continue
                return obj
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}: {e.read().decode()[:80]}"
                if e.code in (429, 400, 502, 503):
                    time.sleep(4 + i * 3); continue
                break
            except urllib.error.URLError as e:
                last_err = f"URLError: {str(e)[:80]}"
                time.sleep(4 + i * 3); continue
            except Exception as e:
                last_err = str(e)[:120]
                time.sleep(3); continue
        print(f"    [LLM提取失败] {last_err}  → 退回空(不丢源, 仅本次未提取)")
        return None
    def _parse(self, obj, scope):
        out = []
        if obj is None:
            return out
        for c in obj.get("captures", []):
            if c.get("layer") == "decision":
                out.append({"layer": "decision", "scope": c.get("scope", scope),
                            "choice": c.get("choice"), "text": c.get("text", ""),
                            "verbatim": c.get("verbatim", ""),
                            "pitfall_text": c.get("pitfall_text")})
            elif c.get("layer") == "fact":
                out.append({"layer": "fact", "kind": c.get("kind", "conclusion"),
                            "scope": c.get("scope", scope), "text": c.get("text", ""),
                            "verbatim": c.get("verbatim", "")})
        return out
    def extract(self, role, content, scope="选型"):
        if not content or not content.strip():
            return []
        messages = [{"role": "system", "content": self.SYSTEM},
                    {"role": "user", "content": f"scope={scope}\n句子：{content}"}]
        return self._parse(self._do(messages), scope)
    def batch_extract(self, items, scope="选型"):
        """异步批处理: 攒一批对话片段, 一次性提炼(根治逐句抖动+过度决策化)。"""
        if not items:
            return []
        buf = "\n".join(f"[{i+1}] ({r}) {c}" for i, (r, c, s) in enumerate(items))
        messages = [{"role": "system", "content": self.SYSTEM_BATCH},
                    {"role": "user", "content": f"scope={scope}\n对话片段:\n{buf}"}]
        return self._parse(self._do(messages), scope)

EXTRACTOR = HeuristicExtractor()   # 默认启发式; 换 LLMExtractor() 即接入真语义(走本机 modelroute)

# ============ 层2 · 冲突仲裁器 (两阶段: 本地候选召回 + 判定) ============
class ConflictArbiter:
    """新记忆进入时, 先仲裁再落库, 取代旧版"同 scope 一刀切 supersede"。
    阶段1(本地/零模型): 同 scope 活跃条目中, 命中 同choice / 互引choice / 共享内容词 的取为候选。
    阶段2(判定): 有模型则 LLM 判 add/skip/update/merge 并区分 改主意/两项目/自相矛盾;
                无模型(默认 demo)走规则兜底(确定性, 覆盖主路径)。
    对齐业界两阶段冲突仲裁, 但阶段1用本地指纹召回(我们站全量立场, 不依赖 RAG 检索)。"""
    STOP = {"采用","决定","方案","处理","本次","当前","因为","我们","选择","使用",
            "模块","重构","回到","需求","做","最贴合","贴合需求","当前采用","回到方案",
            "还是","改用","采用方案","决定用"}
    # 停用 3 字片段: 过滤口语填充词, 避免自由文本误链接(如两个决定都含"的时候")
    # [merge·窗口一 2026-08-05] 自由文本改主意漏仲裁修复
    STOP_TRIG = {"的话","的时候","这件事","这个","那个","什么","怎么","可以","因为","所以",
                "但是","如果","对于","通过","一个","没有","已经","就是","这样","那种","这种",
                "自己","他们","我们","你们","现在","然后","不过","一样","以及","进行","需要",
                "可能","应该","觉得","知道","告诉","问题","时候","东西","出来","起来","这些",
                "那些","一方面","另一方面","所以说","不过也","如果说","也就是","并不是","是不是",
                "的是","了我","了你","了他"}
    def __init__(self, use_llm=False, verbose=False):
        self.use_llm = use_llm
        self.verbose = verbose   # True 时打印 LLM 阶段2判定明细(实验/调试用)
        self.llm = None
        if use_llm:
            try:
                self.llm = LLMExtractor()
            except Exception:
                self.llm = None
                self.use_llm = False
    @staticmethod
    def _tokens(s):
        s = s or ""
        toks = set(re.findall(r'方案[A-Za-z0-9]+', s))
        toks |= {t for t in re.findall(r'[一-鿿]{3,}', s)}
        toks |= {t for t in re.findall(r'[A-Za-z0-9]{3,}', s)}
        return toks - ConflictArbiter.STOP
    @staticmethod
    def _trigrams(s):
        """中文连续段的 3 字片段(子短语), 用于自由文本同主题链接。
        例: '底层的记忆系统了' 与 '其他记忆系统的人' 共享片段(记忆系/忆系统)
        → 链接成同主题, 触发改主意 supersede。整词 _tokens 对不上时的补充通道。"""
        s = s or ""
        trigs = set()
        for run in re.findall(r'[一-鿿]{3,}', s):
            for i in range(len(run) - 2):
                t = run[i:i+3]
                if t not in ConflictArbiter.STOP_TRIG:
                    trigs.add(t)
        return trigs
    @staticmethod
    def _choices(ex):
        c = ex.get("choice")
        return {c} if c else set()
    @staticmethod
    def _choices_from_text(s):
        return set(re.findall(r'方案[A-Za-z0-9]+', s or ""))
    def recall(self, entries, ex):
        """阶段1: 同 scope 活跃条目中, 命中 同choice / 互引choice / 共享内容词 的取为候选。"""
        scope = ex.get("scope")
        new_choices = self._choices(ex)
        new_text = ex.get("text", "") + " " + ex.get("verbatim", "")
        new_toks = self._tokens(new_text)
        cands = []
        for o in entries:
            if o.get("scope") != scope:
                continue
            if o.get("layer") == "decision" and o.get("superseded_by") is not None:
                continue
            o_text = (o.get("text", "") + " " + o.get("verbatim", ""))
            o_toks = self._tokens(o_text)
            hit = False
            if new_choices & self._choices(o):                 # ① 同 choice
                hit = True
            elif (new_choices & self._choices_from_text(o_text)) or \
                 (self._choices(o) & self._choices_from_text(new_text)):   # ② 互引 choice
                hit = True
            elif new_toks & o_toks:                            # ③ 共享 3+ 内容词(整段)
                hit = True
            elif self._trigrams(new_text) & self._trigrams(o_text):
                hit = True                                     # ④ 共享子短语(自由文本同主题) [merge·窗口一]
            if hit:
                cands.append(o)
        return cands
    def judge_decision(self, ex, neighbors):
        if not neighbors:
            return ("add", None)
        new_choices = self._choices(ex)
        new_text = ex.get("text", "") + " " + ex.get("verbatim", "")
        # 确定性守卫(先于 LLM) [⑤真模型实测·窗口二 2026-08-05]: 无歧义场景规则短路,
        # 避免把"同choice重复""显式改主意"也交给模型引入非确定性漂移(实测同一场景三轮判出 skip/merge/add)。
        # 与两窗口规则兜底语义完全一致, 只是把顺序摆正: 守卫 → LLM(仅模糊场景) → 强弱分流兜底。
        for o in neighbors:
            if self._choices(o) & new_choices:                 # 守卫① 同 choice → 重复确认, skip
                if self.verbose:
                    print(f"    [仲裁·守卫①] 同 choice 重复确认 → skip target={o['id']}")
                return ("skip", o["id"])
        for o in neighbors:                                   # 守卫② 显式引用旧 choice → 改主意, 顶掉被引用者
            if self._choices_from_text(new_text) & self._choices(o):
                if self.verbose:
                    print(f"    [仲裁·守卫②] 显式引用旧 choice → update target={o['id']}")
                return ("update", o["id"])
        if self.llm:                                          # 模糊场景(自由文本, 无 choice 引用)才交模型
            return self._judge_llm(ex, neighbors, is_decision=True)
        # [merge·窗口一 2026-08-05] 自由文本(无 choice 引用)分强弱链接, 取代旧版"一律顶掉 neighbors[0]"
        # 强链接(共享>=2 子短语 或 含纠正标记) → 视为改主意 supersede;
        # 弱链接(仅共享 1 个词/片段, 无纠正标记) → 视为不同事项, 并存(避免误杀)。
        new_trigs = self._trigrams(new_text)
        has_correct = bool(re.search(r"(其实|纠正|不是|改成|回退|还是用|改回|不对|没有那么深|想深|换个|换回)",
                                     new_text))
        for o in neighbors:
            o_trigs = self._trigrams(o.get("text", "") + " " + o.get("verbatim", ""))
            if has_correct or len(new_trigs & o_trigs) >= 2:
                return ("update", o["id"])
        return ("add", None)
    def judge_fact(self, ex, neighbors):
        if not neighbors:
            return ("add", None)
        new_text = ex.get("text", "")
        # 确定性守卫(先于 LLM): 同类型且文本重合 → 去重 skip [⑤真模型实测·窗口二 2026-08-05]
        for o in neighbors:
            if ex.get("ftype") == o.get("ftype") and \
               (new_text in o.get("text", "") or o.get("text", "") in new_text):
                if self.verbose:
                    print(f"    [仲裁·守卫] fact 文本重合 → skip target={o['id']}")
                return ("skip", o["id"])
        if self.llm:
            return self._judge_llm(ex, neighbors, is_decision=False)
        return ("add", None)
    def _judge_llm(self, ex, neighbors, is_decision):
        sys = ("你是记忆冲突仲裁器。给定一条待写入的新记忆, 和它同 scope 的最近邻候选(已存在记忆), "
               "判断动作与原因。只输出 JSON(不要说明): "
               '{"action":"add|skip|update|merge","target_id":"eX或无","'
               'kind":"changed_mind|two_projects|self_contradiction|new","reason":"..."}'
               "。add=确属新事项(如不同项目)并存; skip=与候选重复; update=改主意(以 target 为旧决策并标记被取代); "
               "merge=相关, 写入并关联 target。")
        cand_txt = "\n".join(f"- {o['id']} [{o.get('layer')}] {o.get('choice') or ''} {o.get('text','')}"
                             for o in neighbors)
        new_txt = f"新: [{ex.get('layer')}] {ex.get('choice') or ''} {ex.get('text','')} (verbatim: {ex.get('verbatim','')})"
        msg = [{"role": "system", "content": sys},
               {"role": "user", "content": f"{new_txt}\n\n最近邻候选:\n{cand_txt}"}]
        obj = self.llm._do(msg)
        if not obj or "action" not in obj:                    # 模型挂了 → 退回 add(宁多勿错, 不丢)
            return ("add", None)
        if self.verbose:
            print(f"    [仲裁·LLM] action={obj.get('action')} kind={obj.get('kind')} "
                  f"target={obj.get('target_id')} reason={str(obj.get('reason',''))[:40]}")
        act = obj.get("action", "add")
        tid = obj.get("target_id")
        if tid in (None, "无", ""):
            tid = None
        if act == "skip":
            return ("skip", tid or neighbors[0]["id"])
        if act in ("update", "merge"):
            if tid is None or tid not in {o["id"] for o in neighbors}:
                tid = neighbors[0]["id"]
            return (act, tid)
        return ("add", None)

# ============ scope 泛化 (点→面) ============
SCOPE_RANK = {"session": 0, "project": 1, "user": 2, "domain": 3}

def scope_rank(scope):
    """scope 优先级: 越具体越靠前(session>project>user>domain), 无前缀/未知排其后(保持原序)。
    scope 命名约定为自由字符串, 推荐 'session:xxx' / 'project:xxx' / 'user:xxx' / 'domain:xxx'。"""
    prefix = str(scope or "").split(":", 1)[0].lower()
    return SCOPE_RANK.get(prefix, 50)

def _entry_seq(e):
    """条目序号(e10 排在 e9 后), 供稳定排序。"""
    try:
        return int(str(e.get("id", "e0"))[1:])
    except Exception:
        return 0

# ============ 存储层 (可插拔后端) ============
class JsonStore:
    """默认持久化后端: 单 JSON 文件。原子写(tmp+os.replace, 进程崩溃不留半写文件);
    损坏/不可解析 → 当空(故障降级, 不抛飞)。换 SQLite/远端只需实现同名 load/save。"""
    def __init__(self, path=MEMORY_FILE):
        self.path = path
    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return []
        except Exception:
            return []
    def save(self, entries):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

# ============ 层1 · Distiller (双层记忆 + 原始派生 + 异步批处理) ============
class Distiller:
    def __init__(self, path=MEMORY_FILE, extractor=None, use_llm=False, store=None, verbose=False):
        self.store = store or JsonStore(path)   # 存储后端可插拔; 默认单 JSON 文件
        self.path = self.store.path
        self.entries = []
        self.pending = []   # 异步批处理缓冲: ingest 只入这里, flush() 才提取存储
        self.lock = threading.RLock()   # 并发安全: 读-改-写原子, 防 ThreadingHTTPServer 多请求互相覆盖丢写
        self.extractor = extractor or EXTRACTOR   # 实例级注入(不再绑死模块全局); 默认模块级 EXTRACTOR
        self.arbiter = ConflictArbiter(use_llm=use_llm, verbose=verbose)   # 两阶段冲突仲裁(默认规则兜底; use_llm=True 启模型级阶段2)
        self._load()

    def _load(self):
        # 损坏/不可解析 → 当作空(故障降级, 由 JsonStore 兜底), 不抛飞
        self.entries = self.store.load()

    def _save(self):
        self.store.save(self.entries)

    def reload(self):
        self._load()

    def _new_id(self):
        # 基于现有条目最大序号 +1, 不依赖会被 reload 重置的计数器(防 import 残留文件导致 id 跳号)
        nums = [int(e["id"][1:]) for e in self.entries if str(e.get("id", "")).startswith("e")]
        return f"e{(max(nums) if nums else 0) + 1}"

    def add_fact(self, ftype, text, scope, verbatim, evidence=None, refs=None):
        with self.lock:
            eid = self._new_id()
            e = {"id": eid, "layer": "fact", "ftype": ftype, "text": text, "scope": scope,
                 "verbatim": verbatim, "evidence": evidence or verbatim, "refs": refs or [],
                 "freshness": "stable", "created_at": _now()}
            self.entries.append(e)
            self._save()
            return e

    def add_decision(self, scope, choice, text, verbatim, refs=None, supersede_targets=None):
        with self.lock:
            eid = self._new_id()
            if supersede_targets is None:
                # 默认旧行为兜底: 同 scope 全部活跃决策置为"被取代"(改主意场景)
                supersede_targets = [o["id"] for o in self.entries
                                     if o.get("layer") == "decision" and o.get("scope") == scope
                                     and o.get("superseded_by") is None]
            for o in self.entries:
                if o["id"] in supersede_targets:
                    o["superseded_by"] = eid
            e = {"id": eid, "layer": "decision", "scope": scope, "choice": choice,
                 "text": text, "verbatim": verbatim, "refs": refs or [],
                 "superseded_by": None, "created_at": _now()}
            self.entries.append(e)
            self._save()
            self._append_observation(scope, f"决策→{choice}")
            return e

    def _append_observation(self, scope, step):
        with self.lock:
            for o in self.entries:
                if o.get("layer") == "observation" and o.get("scope") == scope:
                    o["journey"].append(step)
                    o["updated_at"] = _now()
                    self._save()
                    return o
            eid = self._new_id()
            e = {"id": eid, "layer": "observation", "scope": scope,
                 "journey": [step], "freshness": "stable",
                 "created_at": _now(), "updated_at": _now()}
            self.entries.append(e)
            self._save()
            return e

    def _resolve_conflict(self, ex):
        """两阶段仲裁: 阶段1 本地候选召回 → 阶段2 判定。返回 (action, target_id)。"""
        with self.lock:
            if ex.get("layer") == "decision":
                neighbors = self.arbiter.recall(self.entries, ex)
                return self.arbiter.judge_decision(ex, neighbors)
            if ex.get("layer") == "fact":
                neighbors = self.arbiter.recall(self.entries, ex)
                return self.arbiter.judge_fact(ex, neighbors)
        return ("add", None)

    def _store_one(self, ex):
        action, target = self._resolve_conflict(ex)
        if action == "skip":
            print(f"    [仲裁·{ex.get('layer')}] 跳过重复: {ex.get('text','')[:24]}")
            return None
        if ex.get("layer") == "fact":
            refs = [target] if (action == "merge" and target) else None
            return self.add_fact(ex["kind"], ex["text"], ex["scope"], ex["verbatim"],
                                 evidence=ex.get("evidence"), refs=refs)
        if ex.get("layer") == "decision":
            supersede = [target] if (action == "update" and target) else []
            fact_eid = None
            if ex.get("pitfall_text"):
                fe = self.add_fact("pitfall", ex["pitfall_text"], ex["scope"], ex["pitfall_text"])
                fact_eid = fe["id"]
            refs = ([fact_eid] if fact_eid else []) + ([target] if (action == "merge" and target) else [])
            return self.add_decision(ex["scope"], ex["choice"], ex["text"], ex["verbatim"],
                                     refs=refs, supersede_targets=supersede)
        return None

    def ingest(self, role, content, scope="选型"):
        """流量侧兜底捕获: 只把句子入 pending 缓冲(异步, 不阻塞请求)。
        真正提取发生在 flush() —— 周期触发, 攒批一次性提炼。"""
        with self.lock:
            self.pending.append((role, content, scope))
        return []

    def flush(self, scope="选型"):
        """异步批处理提取: 把 pending 一次性提炼并存储, 清空缓冲。
        LLMExtractor 走 batch_extract(攒批一次); 无该方法则退化为逐句(Heuristic demo)。"""
        with self.lock:
            if not self.pending:
                return []
            self.reload()
            items = self.pending
            self.pending = []
            out = []
            if hasattr(self.extractor, "batch_extract"):
                for ex in self.extractor.batch_extract(items, scope):
                    e = self._store_one(ex)
                    if e:
                        out.append(e)
            else:
                for (r, c, s) in items:
                    for ex in self.extractor.extract(r, c, s or scope):
                        e = self._store_one(ex)
                        if e:
                            out.append(e)
            return out

    def capture(self, payload):
        """显式捕获契约(形态A 薄垫片): 结构化 → 即时存储; 非结构化 → 即时提取存储(不走 pending, 保证契约即时性)。"""
        with self.lock:
            self.reload()
            if payload.get("layer") in ("fact", "decision", "observation"):
                return self._store_one(payload)
            out = []
            for ex in self.extractor.extract(payload.get("role", "user"), payload.get("content", ""),
                                             payload.get("scope", "选型")):
                e = self._store_one(ex)
                if e:
                    out.append(e)
            return out

    # ---- 回灌 (scope 泛化: 过滤 + 分组 + 优先级; 单 scope 时格式与 v4 完全一致) ----
    def _select(self, scopes=None):
        """选出回灌条目: scopes=None → 全部; 否则只取指定 scope。按 scope 优先级+序号稳定排序。"""
        with self.lock:
            entries = list(self.entries)
        if scopes is not None:
            allow = set(scopes)
            entries = [e for e in entries if e.get("scope") in allow]
        key = lambda e: (scope_rank(e.get("scope")), _entry_seq(e))
        facts = sorted((e for e in entries if e["layer"] == "fact"), key=key)
        decisions = sorted((e for e in entries
                            if e["layer"] == "decision" and e["superseded_by"] is None), key=key)
        obs = sorted((e for e in entries if e["layer"] == "observation"), key=key)
        multi = len({e.get("scope") for e in entries}) > 1
        return facts, decisions, obs, multi

    def snapshot(self, scopes=None):
        facts, decisions, obs, multi = self._select(scopes)
        if not (facts or decisions or obs):
            return ""
        tag = lambda s: f"({s}) " if multi else ""   # 多 scope 才加前缀, 单 scope 保持 v4 原格式
        lines = ["[记忆库 · 强制重注（全量上下文·非检索）]",
                 "（回答若依据下列记忆，请在答案中以 [eN] 引用其 id——读回执用）"]
        lines.append("[当前决策]")
        for d in decisions:
            # choice 缺省(自由文本决策)时退化用 text 直陈, 避免回灌出"当前采用：None" [⑤实测·窗口二]
            if d.get("choice"):
                lines.append(f"  {tag(d.get('scope'))}当前采用：{d['choice']}（{d['text']}）[{d['id']}]")
            else:
                lines.append(f"  {tag(d.get('scope'))}决策：{d['text']}[{d['id']}]")
            if d.get("refs"):
                lines.append(f"  依据事实：{', '.join(d['refs'])}")
        lines.append("[耐久事实]")
        for f in facts:
            lines.append(f"  - {f['id']} {tag(f.get('scope'))}({f['ftype']}): {f['text']}")
        lines.append("[轨迹观察]")
        for o in obs:
            lines.append(f"  OBS({o['scope']}): {' → '.join(o['journey'])}")
        return "\n".join(lines)

    def snapshot_split(self, scopes=None):
        """分层回灌: stable(轨迹 observation, 相对静态常驻 system 前缀, 缓存友好)
        + volatile(决策+事实全量回灌)。站全量立场不漏, 稳定层常驻降本。
        scopes=None → 全部 scope; 多 scope 时按 session>project>user>domain 优先级分组排序。"""
        facts, decisions, obs, multi = self._select(scopes)
        tag = lambda s: f"({s}) " if multi else ""
        stable, volatile = [], []
        if obs:
            stable.append("[轨迹观察·常驻]")
            for o in obs:
                stable.append(f"  OBS({o['scope']}): {' → '.join(o['journey'])}")
        if decisions:
            volatile.append("[当前决策·全量回灌]")
            volatile.append("（依据下列记忆回答时请引用 [eN]——读回执用）")
            for d in decisions:
                # choice 缺省(自由文本决策)时退化用 text 直陈, 避免回灌出"当前采用：None" [⑤实测·窗口二]
                if d.get("choice"):
                    volatile.append(f"  {tag(d.get('scope'))}当前采用：{d['choice']}（{d['text']}）[{d['id']}]")
                else:
                    volatile.append(f"  {tag(d.get('scope'))}决策：{d['text']}[{d['id']}]")
                if d.get("refs"):
                    volatile.append(f"  依据事实：{', '.join(d['refs'])}")
        if facts:
            volatile.append("[耐久事实·全量回灌]")
            for f in facts:
                volatile.append(f"  - {f['id']} {tag(f.get('scope'))}({f['ftype']}): {f['text']}")
        return ("\n".join(stable) if stable else ""), ("\n".join(volatile) if volatile else "")

    def compact(self, max_journey=100):
        """维护: 截断超长轨迹、清除空轨迹 observation(记忆库卫生)。返回清理统计。"""
        with self.lock:
            trimmed, removed, keep = 0, 0, []
            for e in self.entries:
                if e.get("layer") == "observation":
                    j = e.get("journey", [])
                    if not j:
                        removed += 1
                        continue
                    if len(j) > max_journey:
                        e["journey"] = j[-max_journey:]
                        trimmed += 1
                keep.append(e)
            self.entries = keep
            self._save()
            return {"journey_trimmed": trimmed, "observation_removed": removed}

    def mark_used(self, ids):
        """读回执(治"writes to memory, does it read?"): agent 上报本次回答引用的记忆 id,
        累计 used_count / last_used_at。返回命中条数。读覆盖率见 MemoryCore.stats()。"""
        with self.lock:
            idset, hit = set(ids or []), 0
            for e in self.entries:
                if e.get("id") in idset:
                    e["used_count"] = e.get("used_count", 0) + 1
                    e["last_used_at"] = _now()
                    hit += 1
            if hit:
                self._save()
            return hit

# ============ 公开门面 · MemoryCore (形态一 proxy 与形态二 SDK 共用同一入口) ============
class MemoryCore:
    """记忆层核心门面：对外只暴露 ingest / distill / recall / capture 四个动词，
    隐藏 Distiller / ConflictArbiter / Extractor 内部细节。
    - ingest(role, content, scope): 捕获。原始句先落 transcript(源不丢, 容错),
      再入 pending 缓冲(异步, 不阻塞主流程)。
    - distill(scope): 蒸馏+仲裁。pending 攒批一次性提炼, 两阶段仲裁后落库。返回本次入库条目。
    - recall(scopes=None): 回灌。全量快照(决策+事实+轨迹, 非检索);
      scopes 可过滤, 多 scope 按 session>project>user>domain 优先级分组。
    - recall_split(scopes=None): 分层回灌。(stable 常驻前缀, volatile 全量) —— 降本用。
    - capture(payload): 显式捕获契约(结构化直存 / 非结构化即时提取, 不经 pending)。
    - stats() / compact(): 运维自省与记忆库卫生。
    scope 命名自由字符串, 推荐 'session:x' / 'project:x' / 'user:x' / 'domain:x'。
    部署无关: 形态一 proxy 包 HTTP 调它; 形态二 agent 壳 in-process 直接 import 调它。
    故障降级: 记忆库损坏当空库; transcript 写失败只告警, 均不拖垮主流程。"""
    def __init__(self, path=MEMORY_FILE, transcript_path=TRANSCRIPT_FILE,
                 extractor=None, use_llm=False, store=None, verbose=False,
                 scope="选型"):
        self.transcript_path = transcript_path
        self.scope = scope          # 默认 scope: 调用方不传 scope 时用它(适配器场景常用)
        self._store = Distiller(path=path, extractor=extractor, use_llm=use_llm,
                                store=store, verbose=verbose)

    @property
    def d(self):
        """底层 Distiller 直通(适配器/调试用)。常规写入请走 ingest/distill/capture。"""
        return self._store

    # ---- 写侧 ----
    def _log_transcript(self, role, content, scope):
        # 源不丢: 原始句逐句 verbatim 落盘(在提取之前); 记忆库漏了也能从这回捞重建
        try:
            with open(self.transcript_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": _now(), "role": role, "content": content, "scope": scope},
                                   ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"    [降级] 原始留存失败(不影响主流程): {e}")

    def ingest(self, role, content, scope=None):
        """捕获: 原始留存 + 入 pending 缓冲(不提取, 不阻塞)。真正提炼在 distill()。"""
        scope = scope or self.scope
        self._log_transcript(role, content, scope)
        return self._store.ingest(role, content, scope)

    def distill(self, scope=None):
        """蒸馏+仲裁: pending 攒批一次性提炼, 两阶段仲裁后落库。返回本次入库条目列表。"""
        return self._store.flush(scope or self.scope)

    def capture(self, payload):
        """显式捕获契约: 结构化(fact/decision/observation)即时直存;
        非结构化(role/content)即时提取存储(不经 pending, 保证契约即时性)。"""
        return self._store.capture(payload)

    # ---- 读侧 ----
    def recall(self, scopes=None, split=False):
        """回灌: 全量快照(单块文本, 站全量立场不漏, 非 RAG 检索)。
        scopes=None → 全部; 传列表则只取指定 scope(多 scope 分组排序)。
        split=True → 返回 (stable, volatile) 分层, 等价 recall_split(兼容适配器写法)。"""
        if split:
            return self._store.snapshot_split(scopes)
        return self._store.snapshot(scopes)

    def recall_split(self, scopes=None):
        """分层回灌: (stable 常驻前缀, volatile 全量)。稳定层缓存友好降本。"""
        return self._store.snapshot_split(scopes)

    def mark_used(self, ids):
        """读回执: agent 上报本次回答引用的记忆 id(回答里出现的 [eN])。返回命中条数。"""
        return self._store.mark_used(ids)

    # ---- 运维 ----
    @property
    def entries(self):
        """只读自省(demo/调试用): 当前库内条目。写入请走 ingest/distill/capture。"""
        return self._store.entries

    def get_entries(self):
        """兼容别名(适配器写法): 等价 .entries。"""
        return self._store.entries

    def get_arbiter(self):
        """兼容别名(适配器写法): 取仲裁器实例(自省/调参用)。"""
        return self._store.arbiter

    def stats(self):
        """自省: 条目数/分层分布/scope 分布/活跃决策数/pending 深度/
        读覆盖率(read_coverage=被引用过的条目占比, 回答"does it read?")/从未被读数。"""
        es = self._store.entries
        by_layer, by_scope = {}, {}
        read = 0
        for e in es:
            by_layer[e.get("layer")] = by_layer.get(e.get("layer"), 0) + 1
            by_scope[e.get("scope")] = by_scope.get(e.get("scope"), 0) + 1
            if e.get("used_count", 0) > 0:
                read += 1
        active = sum(1 for e in es
                     if e.get("layer") == "decision" and e.get("superseded_by") is None)
        return {"entries": len(es), "by_layer": by_layer, "by_scope": by_scope,
                "active_decisions": active, "pending": len(self._store.pending),
                "read_coverage": round(read / len(es), 3) if es else 0,
                "never_read": len(es) - read}

    def compact(self, max_journey=100):
        """记忆库卫生: 截断超长轨迹、清除空轨迹 observation。返回清理统计。"""
        return self._store.compact(max_journey)

    def rebuild(self, transcript_path=None, rebuilt_suffix=".rebuilt"):
        """源不丢闭环(可验证): 从 transcript 逐句重放(捕获→蒸馏→仲裁) 到一个新库,
        不动现有库。返回重建出的 MemoryCore 实例——记忆库丢了/坏了, 从源回捞即重建。"""
        tp = transcript_path or self.transcript_path
        new = MemoryCore(path=self._store.path + rebuilt_suffix,
                         transcript_path=tp + rebuilt_suffix,
                         extractor=self._store.extractor,
                         use_llm=self._store.arbiter.use_llm)
        n = 0
        with open(tp, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue   # 坏行跳过(降级), 不阻塞重建
                new.ingest(r.get("role", "user"), r.get("content", ""),
                           r.get("scope", "选型"))
                n += 1
        new.distill()
        new.replayed = n   # 自省: 本次重放句数
        return new

    def reload(self):
        """多实例/多进程共享同一库文件时, 读前 reload 防内存态陈旧。"""
        self._store.reload()

    def _load(self):
        """内部: 加载条目到内存(兼容适配器写法)。"""
        self._store.reload()
        return list(self._store.entries)

    def _save(self, entries=None):
        """内部: 写回存储(兼容适配器写法)。entries=None 则保存当前 self.entries。"""
        if entries is not None:
            self._store.entries = entries
        self._store._save()

    def reset(self):
        """清空记忆库(demo/测试用)。transcript 不动(源不丢)。"""
        self._store.entries = []
        self._store.pending = []
        self._store._save()

