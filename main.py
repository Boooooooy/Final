from crewai import Crew, Task
from pymilvus import connections
import os, time, threading
from typing import Optional

from HealthBot.agent import create_health_companion, create_guardrail_agent, finalize_session, build_prompt_from_redis
from toolkits.redis_store import (
    try_register_request, make_request_id, append_round, peek_next_n,
    append_audio_segment, read_and_clear_audio_segments, get_audio_result, set_audio_result,
    get_redis, set_state_if, xadd_alert
)
import hashlib
from toolkits.tools import summarize_chunk_and_commit

SUMMARY_CHUNK_SIZE = int(os.getenv("SUMMARY_CHUNK_SIZE", 5))

class AgentManager:
    def __init__(self):
        self.guardrail_agent = create_guardrail_agent()
        self.health_agent_cache = {}
    def get_guardrail(self): return self.guardrail_agent
    def get_health_agent(self, user_id: str):
        if user_id not in self.health_agent_cache:
            self.health_agent_cache[user_id] = create_health_companion(user_id)
        return self.health_agent_cache[user_id]
    def release_health_agent(self, user_id: str):
        if user_id in self.health_agent_cache: del self.health_agent_cache[user_id]

# ---- Persist & maybe summarize ----

def log_session(user_id: str, query: str, reply: str, request_id: Optional[str] = None):
    rid = request_id or make_request_id(user_id, query)
    if not try_register_request(user_id, rid):
        print("[去重] 跳過重複請求"); return
    append_round(user_id, {"input": query, "output": reply, "rid": rid})
    # 嘗試抓下一段 5 輪（不足會回空）→ LLM 摘要 → CAS 提交
    start, chunk = peek_next_n(user_id, SUMMARY_CHUNK_SIZE)
    if start is not None and chunk:
        summarize_chunk_and_commit(user_id, start_round=start, history_chunk=chunk)

# ---- Pipeline ----

def handle_user_message(agent_manager: AgentManager, user_id: str, query: str,
                        audio_id: Optional[str] = None, is_final: bool = True) -> str:
    # 0) 統一音檔 ID（沒帶就用文字 hash 當臨時 ID，向後相容）
    audio_id = audio_id or hashlib.sha1(query.encode("utf-8")).hexdigest()[:16]

    # 1) 非 final：不觸發任何 LLM/RAG/通報，只緩衝片段
    if not is_final:
        append_audio_segment(user_id, audio_id, query)
        return "👌 已收到語音片段"

    # 2) 音檔級鎖：一次且只一次處理同一段音檔
    lock_id = f"{user_id}#audio:{audio_id}"
    if not set_state_if(lock_id, expect="", to="PROCESSING"):
        # 可能已處理或處理中 → 回快取或提示
        cached = get_audio_result(user_id, audio_id)
        return cached or "我正在處理你的語音，請稍等一下喔。"

    try:
        # 3) 合併之前緩衝的 partial → 最終要處理的全文
        head = read_and_clear_audio_segments(user_id, audio_id)
        full_text = (head + " " + query).strip() if head else query

        # 4) 原本流程：先 guardrail，再 health agent（你現有碼原封搬過來）
        # 設置環境變數供工具使用
        os.environ["CURRENT_USER_ID"] = user_id
        
        guard = agent_manager.get_guardrail()
        guard_task = Task(
            description=(
                f"判斷是否需要攔截：「{full_text}」。"
                "務必使用 model_guardrail 工具進行判斷；"
                "安全回 OK；需要攔截時回 BLOCK: <原因>（僅此兩種）。"
            ),
            expected_output="OK 或 BLOCK: <原因>",
            agent=guard
        )
        guard_res = (Crew(agents=[guard], tasks=[guard_task], verbose=False).kickoff().raw or "").strip()
        if guard_res.startswith("BLOCK:"):
            reason = guard_res[6:].strip()
            # 檢查是否涉及自傷風險，需要通報個管師
            if any(k in reason for k in ["自傷", "自殺", "傷害自己", "緊急"]):
                xadd_alert(user_id=user_id, reason=f"可能自傷風險：{full_text}", severity="high")
            reply = "抱歉，這個問題涉及違規或需專業人士評估，我無法提供解答。"
            set_audio_result(user_id, audio_id, reply)
            log_session(user_id, full_text, reply)
            return reply

        care = agent_manager.get_health_agent(user_id)
        ctx = build_prompt_from_redis(user_id, k=6, current_input=full_text)
        task = Task(
            description=f"{ctx}\n\n使用者輸入：{full_text}\n請以台語風格溫暖務實回覆；有需要查看COPD相關資料或緊急事件需要通報時，請使用工具。",
            expected_output="台語風格的溫暖關懷回覆，必要時使用工具。",
            agent=care,
        )
        res = (Crew(agents=[care], tasks=[task], verbose=False).kickoff().raw or "")

        # 5) 結果快取 + 落歷史（你原本就有 log_session）
        set_audio_result(user_id, audio_id, res)
        log_session(user_id, full_text, res)
        return res

    finally:
        set_state_if(lock_id, expect="PROCESSING", to="FINALIZED")

class UserSession:
    def __init__(self, user_id: str, agent_manager: AgentManager, timeout: int = 300):
        self.user_id = user_id; self.agent_manager = agent_manager; self.timeout = timeout
        self.last_active_time = None; self.stop_event = threading.Event()
        threading.Thread(target=self._watchdog, daemon=True).start()
    def update_activity(self): self.last_active_time = time.time()
    def _watchdog(self):
        while not self.stop_event.is_set():
            time.sleep(5)
            if self.last_active_time and (time.time() - self.last_active_time > self.timeout):
                print(f"\n⏳ 閒置超過 {self.timeout}s，開始收尾...")
                finalize_session(self.user_id)
                self.agent_manager.release_health_agent(self.user_id)
                self.stop_event.set()

def main():
    connections.connect(alias="default", uri=os.getenv("MILVUS_URI", "http://localhost:19530"))
    am = AgentManager(); uid = os.getenv("TEST_USER_ID", "test_user")
    sess = UserSession(uid, am)
    print("✅ 啟動完成，閒置 5 分鐘：補分段摘要→Refine→Purge")
    try:
        am.get_health_agent(uid)
        while not sess.stop_event.is_set():
            try:
                q = input("🧓 長輩：").strip()
            except (KeyboardInterrupt, EOFError): break
            if not q: continue
            sess.update_activity()
            a = handle_user_message(am, uid, q)
            print("👧 金孫：", a)
    finally:
        if not sess.stop_event.is_set():
            print("\n📝 結束對話：收尾...")
            finalize_session(uid)
        am.release_health_agent(uid)
        print("👋 系統已關閉")

if __name__ == "__main__":
    from dotenv import load_dotenv; load_dotenv(); main()