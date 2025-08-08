from crewai import Crew, Task
from pymilvus import connections
import os, time, threading

from HealthBot.agent import create_health_companion, create_guardrail_agent, finalize_session, build_prompt_from_redis
from toolkits.redis_store import try_register_request, make_request_id, append_round, peek_next_n
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

def log_session(user_id: str, query: str, reply: str, request_id: str | None = None):
    rid = request_id or make_request_id(user_id, query)
    if not try_register_request(user_id, rid):
        print("[去重] 跳過重複請求"); return
    append_round(user_id, {"input": query, "output": reply, "rid": rid})
    # 嘗試抓下一段 5 輪（不足會回空）→ LLM 摘要 → CAS 提交
    start, chunk = peek_next_n(user_id, SUMMARY_CHUNK_SIZE)
    if start is not None and chunk:
        summarize_chunk_and_commit(user_id, start_round=start, history_chunk=chunk)

# ---- Pipeline ----

def handle_user_message(agent_manager: AgentManager, user_id: str, query: str) -> str:
    guard = agent_manager.get_guardrail()
    guard_task = Task(description=f"判斷是否危險：「{query}」。安全回 OK；危險回 BLOCK: <原因>", expected_output="OK 或 BLOCK: <原因>", agent=guard)
    guard_res = (Crew(agents=[guard], tasks=[guard_task], verbose=False).kickoff().raw or "").strip()
    if guard_res.startswith("BLOCK:"): return f"🚨 系統攔截：{guard_res[6:].strip()}"

    care = agent_manager.get_health_agent(user_id)
    ctx = build_prompt_from_redis(user_id, k=6)
    task = Task(
        description=f"{ctx}\n\n使用者輸入：{query}\n請以台語風格溫暖務實回覆；必要時使用工具。",
        expected_output="台語風格的溫暖關懷回覆，必要時使用工具。",
        agent=care,
    )
    res = (Crew(agents=[care], tasks=[task], verbose=False).kickoff().raw or "")
    log_session(user_id, query, res)
    return res

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