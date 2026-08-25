from functools import lru_cache
from pathlib import Path
import os

from pydantic import BaseModel

from app.schemas.task_reward import (
    TASK_REWARD_CONFIGURATION_VERSION,
    TaskRewardPoolConfig,
)
from app.schemas.report_quality import (
    REPORT_QUALITY_CONFIGURATION_VERSION,
    REPORT_QUALITY_POLICY_VERSION,
    ReportQualityConfig,
)
from app.schemas.task_finding_reward import (
    TASK_FINDING_CONFIGURATION_VERSION,
    TASK_FINDING_POLICY_VERSION,
    FindingAllocationScope,
    SeverityRewardWeightConfig,
    TaskFindingRewardConfig,
    UniquenessRewardConfig,
)
from app.schemas.task_operator_reward import (
    TASK_OPERATOR_CONFIGURATION_VERSION,
    TASK_OPERATOR_POLICY_VERSION,
    ChiefFinderConfig,
    DuplicateRewardConfig,
    TaskOperatorRewardConfig,
)
from app.schemas.week7_reward_cycle import (
    WEEK7_REWARD_CONFIGURATION_VERSION,
    WEEK7_REWARD_POLICY_VERSION,
)


class Settings(BaseModel):
    app_name: str = "Proofguard Audit API"
    project_root: Path = Path(__file__).resolve().parents[2]
    data_dir: Path = Path("data/audits")
    protocol_data_dir: Path = Path("data/protocol")
    sqlite_path: Path = Path("data/audit_api.sqlite3")
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    demo_mode: bool = False
    task_reward_configuration_version: str = TASK_REWARD_CONFIGURATION_VERSION
    task_reward_pool: TaskRewardPoolConfig = TaskRewardPoolConfig()
    report_quality_configuration_version: str = REPORT_QUALITY_CONFIGURATION_VERSION
    report_quality_policy_version: str = REPORT_QUALITY_POLICY_VERSION
    report_quality: ReportQualityConfig = ReportQualityConfig()
    task_finding_configuration_version: str = TASK_FINDING_CONFIGURATION_VERSION
    task_finding_policy_version: str = TASK_FINDING_POLICY_VERSION
    task_finding_reward: TaskFindingRewardConfig = TaskFindingRewardConfig()
    task_operator_configuration_version: str = TASK_OPERATOR_CONFIGURATION_VERSION
    task_operator_policy_version: str = TASK_OPERATOR_POLICY_VERSION
    task_operator_reward: TaskOperatorRewardConfig = TaskOperatorRewardConfig()
    week7_reward_cycle_configuration_version: str = (
        WEEK7_REWARD_CONFIGURATION_VERSION
    )
    week7_reward_cycle_policy_version: str = WEEK7_REWARD_POLICY_VERSION

    @property
    def absolute_data_dir(self) -> Path:
        return self._resolve_under_project(self.data_dir)

    @property
    def absolute_sqlite_path(self) -> Path:
        return self._resolve_under_project(self.sqlite_path)

    @property
    def absolute_protocol_data_dir(self) -> Path:
        return self._resolve_under_project(self.protocol_data_dir)

    def display_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.project_root).as_posix()
        except ValueError:
            return path.name

    def _resolve_under_project(self, path: Path) -> Path:
        if path.is_absolute():
            return path
        return (self.project_root / path).resolve()


@lru_cache
def get_settings() -> Settings:
    data_dir = Path(os.getenv("AUDIT_API_DATA_DIR", "data/audits"))
    protocol_data_dir = Path(os.getenv("AUDIT_API_PROTOCOL_DATA_DIR", "data/protocol"))
    sqlite_path = Path(os.getenv("AUDIT_API_SQLITE_PATH", "data/audit_api.sqlite3"))
    cors_origins = [
        origin.strip()
        for origin in os.getenv(
            "AUDIT_API_CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173",
        ).split(",")
        if origin.strip()
    ]
    task_reward_pool = TaskRewardPoolConfig(
        miner_share=os.getenv("AUDIT_API_TASK_REWARD_MINER_SHARE", "0.70"),
        validator_share=os.getenv("AUDIT_API_TASK_REWARD_VALIDATOR_SHARE", "0.20"),
        protocol_share=os.getenv("AUDIT_API_TASK_REWARD_PROTOCOL_SHARE", "0.10"),
    )
    report_quality = ReportQualityConfig(
        correctness=os.getenv("AUDIT_API_REPORT_QUALITY_CORRECTNESS_WEIGHT", "0.35"),
        poc_quality=os.getenv("AUDIT_API_REPORT_QUALITY_POC_WEIGHT", "0.25"),
        root_cause=os.getenv("AUDIT_API_REPORT_QUALITY_ROOT_CAUSE_WEIGHT", "0.20"),
        impact=os.getenv("AUDIT_API_REPORT_QUALITY_IMPACT_WEIGHT", "0.10"),
        fix=os.getenv("AUDIT_API_REPORT_QUALITY_FIX_WEIGHT", "0.10"),
    )
    task_finding_reward = TaskFindingRewardConfig(
        severity_weights=SeverityRewardWeightConfig(
            critical=os.getenv("AUDIT_API_FINDING_CRITICAL_WEIGHT", "16"),
            high=os.getenv("AUDIT_API_FINDING_HIGH_WEIGHT", "8"),
            medium=os.getenv("AUDIT_API_FINDING_MEDIUM_WEIGHT", "3"),
            low=os.getenv("AUDIT_API_FINDING_LOW_WEIGHT", "1"),
            informational=os.getenv("AUDIT_API_FINDING_INFORMATIONAL_WEIGHT", "0"),
        ),
        uniqueness=UniquenessRewardConfig(
            coefficient=os.getenv("AUDIT_API_FINDING_UNIQUENESS_COEFFICIENT", "0.20"),
            floor=os.getenv("AUDIT_API_FINDING_UNIQUENESS_FLOOR", "0.50"),
        ),
        default_allocation_scope=os.getenv(
            "AUDIT_API_FINDING_ALLOCATION_SCOPE",
            FindingAllocationScope.CATEGORY_ISOLATED.value,
        ),
    )
    task_operator_reward = TaskOperatorRewardConfig(
        duplicates=DuplicateRewardConfig(
            top_k=os.getenv("AUDIT_API_DUPLICATE_REWARD_TOP_K", "5"),
        ),
        chief_finder=ChiefFinderConfig(
            bonus_percentage=os.getenv("AUDIT_API_CHIEF_FINDER_BONUS_PERCENTAGE", "0.05"),
            quality_percentage=os.getenv(
                "AUDIT_API_QUALITY_POOL_PERCENTAGE", "0.95"
            ),
            quality_threshold=os.getenv("AUDIT_API_CHIEF_FINDER_QUALITY_THRESHOLD", "0.80"),
        ),
    )
    return Settings(
        data_dir=data_dir,
        protocol_data_dir=protocol_data_dir,
        sqlite_path=sqlite_path,
        cors_origins=cors_origins,
        demo_mode=os.getenv("PROOFGUARD_DEMO_MODE", "0"),
        task_reward_configuration_version=os.getenv(
            "AUDIT_API_TASK_REWARD_CONFIGURATION_VERSION",
            TASK_REWARD_CONFIGURATION_VERSION,
        ),
        task_reward_pool=task_reward_pool,
        report_quality_configuration_version=os.getenv(
            "AUDIT_API_REPORT_QUALITY_CONFIGURATION_VERSION",
            REPORT_QUALITY_CONFIGURATION_VERSION,
        ),
        report_quality_policy_version=os.getenv(
            "AUDIT_API_REPORT_QUALITY_POLICY_VERSION",
            REPORT_QUALITY_POLICY_VERSION,
        ),
        report_quality=report_quality,
        task_finding_configuration_version=os.getenv(
            "AUDIT_API_TASK_FINDING_CONFIGURATION_VERSION",
            TASK_FINDING_CONFIGURATION_VERSION,
        ),
        task_finding_policy_version=os.getenv(
            "AUDIT_API_TASK_FINDING_POLICY_VERSION",
            TASK_FINDING_POLICY_VERSION,
        ),
        task_finding_reward=task_finding_reward,
        task_operator_configuration_version=os.getenv(
            "AUDIT_API_TASK_OPERATOR_CONFIGURATION_VERSION",
            TASK_OPERATOR_CONFIGURATION_VERSION,
        ),
        task_operator_policy_version=os.getenv(
            "AUDIT_API_TASK_OPERATOR_POLICY_VERSION",
            TASK_OPERATOR_POLICY_VERSION,
        ),
        task_operator_reward=task_operator_reward,
        week7_reward_cycle_configuration_version=os.getenv(
            "AUDIT_API_WEEK7_REWARD_CYCLE_CONFIGURATION_VERSION",
            WEEK7_REWARD_CONFIGURATION_VERSION,
        ),
        week7_reward_cycle_policy_version=os.getenv(
            "AUDIT_API_WEEK7_REWARD_CYCLE_POLICY_VERSION",
            WEEK7_REWARD_POLICY_VERSION,
        ),
    )
