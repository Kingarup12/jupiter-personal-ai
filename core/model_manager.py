from enum import Enum, auto
import time
from core.logger import Logger

class Task(Enum):
    MAIN_CHAT = auto()
    EXECUTIVE = auto()
    EMOTION = auto()
    PERSONALITY = auto()
    CURIOSITY = auto()
    RELATIONSHIP = auto()
    MEMORY_ANALYZER = auto()
    DUPLICATE = auto()
    CONTRADICTION = auto()
    MEMORY_RETRIEVAL = auto()
    VALIDATOR = auto()
    REWRITER = auto()
    VOICE_CLEANUP = auto()
    TTS = auto()
    IMAGE_PROMPT = auto()

class ModelManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ModelManager, cls).__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self):
        self.last_used_model = 'unknown'
        self.stats = {}  
        from config.settings import TASK_MODELS
        self.task_models = TASK_MODELS

    def _get_stats(self, model: str):
        if model not in self.stats:
            self.stats[model] = {
                'requests_per_minute': 0,
                'requests_today': 0,
                'tokens_per_minute': 0,
                'tokens_today': 0,
                'avg_latency': 0.0,
                'consecutive_failures': 0,
                'last_429': 0.0,
                'cooldown_expiration': 0.0,
                'health_status': 'Healthy',
                'total_requests': 0
            }
        return self.stats[model]

    def is_healthy(self, model: str) -> bool:
        s = self._get_stats(model)
        
        # Cooldown check: if failed recently, wait 60 seconds
        if s['cooldown_expiration'] > time.time():
            s['health_status'] = 'Cooldown (429)'
            return False
            
        if s['consecutive_failures'] >= 3:
            if time.time() - s['last_429'] < 60:
                s['cooldown_expiration'] = time.time() + 60
                s['health_status'] = 'Consecutive Failures'
                return False
            else:
                # Reset after cooldown
                s['consecutive_failures'] = 0
                s['health_status'] = 'Healthy' 
                
        # Basic daily quota assumption based on model type
        max_requests = 1500
        if "pro" in model:
            max_requests = 50
        elif "lite" in model:
            max_requests = 3000
            
        if s['requests_today'] >= max_requests:
            return False
            
        return True

    def get_best_models(self, task: Task) -> list[str]:
        task_key = task.name
        candidates = self.task_models.get(task_key, [])
        healthy_candidates = []
        
        for m in candidates:
            if self.is_healthy(m):
                healthy_candidates.append(m)
        
        if not healthy_candidates:
            Logger.warning(f"[Routing] Task: {task_key} | All candidates unhealthy. Falling back to default list.")
            return candidates
            
        # Adaptive Sorting: Prefer the fastest healthy model if we have latency data (otherwise keep priority order)
        # We only sort if we have actually measured latency, else we preserve the config priority list.
        measured = [m for m in healthy_candidates if self.stats[m]['avg_latency'] > 0]
        if measured:
            healthy_candidates.sort(key=lambda m: self.stats[m]['avg_latency'] if self.stats[m]['avg_latency'] > 0 else 999.0)
            
        selected = healthy_candidates[0]
        
        # Determine reason
        reason = "Highest priority healthy model"
        if selected != candidates[0]:
            skipped = candidates[0]
            if self.stats.get(skipped, {}).get('health_status') == 'Cooldown (429)':
                reason = f"{skipped} in cooldown (429)"
            else:
                reason = f"Primary models unhealthy/exhausted"
        elif self.stats[selected]['avg_latency'] > 0:
            reason = "Fastest healthy model"
            
        log_msg = f"Task: {task_key}\n\nCandidates:\n"
        for m in candidates:
            log_msg += f"- {m}\n"
        log_msg += f"\nSelected:\n{selected}\n\nReason:\n{reason}"
        
        Logger.info(f"[Routing Decision]\n{log_msg}")
        
        return healthy_candidates

    def report_success(self, model: str, latency: float):
        self.last_used_model = model
        s = self._get_stats(model)
        s['consecutive_failures'] = 0
        s['requests_today'] += 1
        s['total_requests'] += 1
        
        # Exponential moving average for latency
        if s['avg_latency'] == 0.0:
            s['avg_latency'] = latency
        else:
            s['avg_latency'] = 0.8 * s['avg_latency'] + 0.2 * latency
            
        Logger.info(f"[ModelManager] {model} succeeded (latency: {latency:.2f}s, reqs_today: {s['requests_today']})")

    def report_failure(self, model: str, is_429: bool):
        s = self._get_stats(model)
        s['consecutive_failures'] += 1
        if is_429:
            s['last_429'] = time.time()
        Logger.warning(f"[ModelManager] {model} failed (429: {is_429}, consecutive: {s['consecutive_failures']})")
