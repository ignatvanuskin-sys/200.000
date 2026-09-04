"""store.py — состояние webhook/API.

Дизайн:
  * быстрый sync API (только RAM-операции под RLock — микросекунды, event loop не блокируется);
  * Redis используется ТОЛЬКО для атомарного dedupe (SET NX EX) — переживает
    рестарт и работает между репликами; любая ошибка Redis → fallback в RAM + лог;
  * history/rate — RAM с TTL, maxsize и локами (честно: для multi-replica нужен
    Redis и для них; см. LIMITATIONS);
  * без REDIS_URL приложение работает на 1 реплике (прод сейчас: 1 replica, AMS).

LIMITATIONS (не выдавать за durable queue):
  - BackgroundTasks не переживают SIGTERM/restart;
  - history/rate в RAM: рестарт = потеря, 2 реплики = расхождение.
"""
import logging
import threading
import time
from collections import defaultdict, deque

log = logging.getLogger("implant-dent.store")

MAX_KEYS = 20000  # жёсткий потолок числа ключей на структуру
_SWEEP_INTERVAL_S = 60.0  # периодический sweep seen, не на каждый запрос


class ProdStore:
    def __init__(self, dedupe_ttl_s=300, rate_limit=10, rate_window_s=60,
                 history_max=10, history_ttl_s=86400, redis_url=""):
        self._lock = threading.RLock()
        self._seen: dict[str, float] = {}          # msg_id -> ts (claim)
        self._rates: dict[str, deque] = defaultdict(deque)  # scope:key -> timestamps
        self._hist: dict[str, list] = {}           # phone -> [{role, content, ts}]
        self._last_sweep = 0.0
        self.dedupe_ttl_s = dedupe_ttl_s
        self.rate_limit = rate_limit
        self.rate_window_s = rate_window_s
        self.history_max = history_max
        self.history_ttl_s = history_ttl_s
        self._redis = None
        if redis_url:
            try:
                import redis as redis_lib
                self._redis = redis_lib.Redis.from_url(redis_url, socket_timeout=0.5)
                self._redis.ping()
                log.info("redis dedupe backend enabled")
            except Exception as e:
                log.warning("redis unavailable, memory fallback: %s", str(e)[:150])
                self._redis = None

    # ---------- internal ----------
    def _evict_if_needed(self, d: dict) -> None:
        if len(d) > MAX_KEYS:
            # срезать самые старые (по ts где возможно, иначе первые)
            try:
                if d is self._seen:
                    for k in sorted(d, key=d.get)[: len(d) - MAX_KEYS]:
                        del d[k]
                else:
                    for k in list(d)[: len(d) - MAX_KEYS]:
                        del d[k]
            except Exception:
                pass

    def _sweep_seen_locked(self, now: float) -> None:
        if now - self._last_sweep < _SWEEP_INTERVAL_S:
            return
        self._last_sweep = now
        ttl = self.dedupe_ttl_s
        for k in [k for k, t in self._seen.items() if now - t > ttl]:
            del self._seen[k]

    # ---------- dedupe: True = новый (claim успешен), False = дубликат ----------
    def claim_seen(self, msg_id: str) -> bool:
        if not msg_id:
            return True
        now = time.time()
        if self._redis is not None:
            try:
                ok = self._redis.set(f"wa:seen:{msg_id}", "1", nx=True, ex=self.dedupe_ttl_s)
                return bool(ok)
            except Exception as e:
                log.warning("redis claim failed, memory fallback: %s", str(e)[:120])
        with self._lock:
            self._sweep_seen_locked(now)
            if msg_id in self._seen:
                return False
            self._seen[msg_id] = now
            self._evict_if_needed(self._seen)
            return True

    # ---------- rate limit: True = превышен ----------
    def is_rate_limited(self, scope: str, key: str, limit: int = 0, window_s: int = 0) -> bool:
        now = time.time()
        limit = limit or self.rate_limit
        window = window_s or self.rate_window_s
        ck = f"{scope}:{key}"
        with self._lock:
            q = self._rates[ck]
            while q and q[0] < now - window:
                q.popleft()
            if len(q) >= limit:
                return True
            q.append(now)
            if len(self._rates) > MAX_KEYS:
                self._rates.pop(next(iter(self._rates)))
            return False

    # ---------- history ----------
    def push_history(self, phone: str, role: str, content: str) -> None:
        with self._lock:
            arr = self._hist.get(phone, [])
            arr.append({"role": role, "content": content, "ts": time.time()})
            if len(arr) > self.history_max:
                del arr[0: len(arr) - self.history_max]
            self._hist[phone] = arr
            self._evict_if_needed(self._hist)

    def get_history(self, phone: str) -> list:
        now = time.time()
        with self._lock:
            arr = self._hist.get(phone, [])
            fresh = [m for m in arr if now - m.get("ts", 0) < self.history_ttl_s]
            if len(fresh) != len(arr):
                if fresh:
                    self._hist[phone] = fresh
                else:
                    self._hist.pop(phone, None)
            return [{"role": m["role"], "content": m["content"]} for m in fresh]

    def stats(self) -> dict:
        with self._lock:
            return {"seen": len(self._seen), "rates": len(self._rates),
                    "histories": len(self._hist), "redis": self._redis is not None}

    def reset(self) -> None:
        """Только для тестов: очистить всё состояние."""
        with self._lock:
            self._seen.clear()
            self._rates.clear()
            self._hist.clear()
