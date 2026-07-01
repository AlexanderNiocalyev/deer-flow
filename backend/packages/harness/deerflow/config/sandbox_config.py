from pydantic import BaseModel, ConfigDict, Field


class VolumeMountConfig(BaseModel):
    """Configuration for a volume mount."""

    host_path: str = Field(
        ...,
        description=(
            "Source path for the mount. Resolution depends on the active provider: "
            "``LocalSandboxProvider`` checks this path from the gateway process — in "
            "``make dev`` that is the host machine, but in Docker deployments "
            "(``make up`` / docker-compose) it is the path *inside* the "
            "``deer-flow-gateway`` container, so the host directory must also be "
            "bind-mounted into the gateway service for the mount to take effect. "
            "``AioSandboxProvider`` (DooD) passes this value straight to ``docker -v`` "
            "for the sandbox container, where it is resolved by the host Docker daemon "
            "from the host machine's perspective."
        ),
    )
    container_path: str = Field(..., description="Path inside the container")
    read_only: bool = Field(default=False, description="Whether the mount is read-only")


class SandboxConfig(BaseModel):
    """Config section for a sandbox.

    Common options:
        use: Class path of the sandbox provider (required)
        allow_host_bash: Enable host-side bash execution for LocalSandboxProvider.
            Dangerous and intended only for fully trusted local workflows.

    AioSandboxProvider specific options:
        image: Docker image to use (default: enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest)
        port: Base port for sandbox containers (default: 8080)
        replicas: Maximum number of concurrent sandbox containers (default: 3). When the limit is reached the least-recently-used sandbox is evicted to make room.
        container_prefix: Prefix for container names (default: deer-flow-sandbox)
        idle_timeout: Idle timeout in seconds before sandbox is released (default: 600 = 10 minutes). Set to 0 to disable.
        mounts: List of volume mounts to share directories with the container
        environment: Environment variables to inject into the container (values starting with $ are resolved from host env)

    VercelSandboxProvider specific options:
        vercel_token: Vercel API token, or `$ENV_VAR` reference
        vercel_project_id: Vercel project id, or `$ENV_VAR` reference
        vercel_team_id: Optional Vercel team id, or `$ENV_VAR` reference
        vercel_runtime: Runtime name passed to Vercel Sandbox (default: python3.13)
        vercel_vcpus: Vercel resource vCPU count (default: 2)
        vercel_memory_mb: Vercel memory in MiB. Must equal vercel_vcpus * 2048.
        vercel_stop_on_release: Stop persistent sandboxes after each agent run so Vercel can snapshot and idle.
        vercel_record_store: Where to persist DeerFlow sandbox id -> Vercel sandbox id mappings (`auto`, `database`, or `file`).
    """

    use: str = Field(
        ...,
        description="Class path of the sandbox provider (e.g. deerflow.sandbox.local:LocalSandboxProvider)",
    )
    allow_host_bash: bool = Field(
        default=False,
        description="Allow the bash tool to execute directly on the host when using LocalSandboxProvider. Dangerous; intended only for fully trusted local environments.",
    )
    image: str | None = Field(
        default=None,
        description="Docker image to use for the sandbox container",
    )
    port: int | None = Field(
        default=None,
        description="Base port for sandbox containers",
    )
    replicas: int | None = Field(
        default=None,
        description="Maximum number of concurrent sandbox containers (default: 3). When the limit is reached the least-recently-used sandbox is evicted to make room.",
    )
    container_prefix: str | None = Field(
        default=None,
        description="Prefix for container names",
    )
    idle_timeout: int | None = Field(
        default=None,
        description="Idle timeout in seconds before sandbox is released (default: 600 = 10 minutes). Set to 0 to disable.",
    )
    mounts: list[VolumeMountConfig] = Field(
        default_factory=list,
        description="List of volume mounts to share directories between host and container",
    )
    environment: dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables to inject into the sandbox container. Values starting with $ will be resolved from host environment variables.",
    )

    vercel_token: str | None = Field(
        default=None,
        description="Vercel API token for VercelSandboxProvider. Values starting with $ are resolved from host environment variables.",
    )
    vercel_project_id: str | None = Field(
        default=None,
        description="Vercel project id for VercelSandboxProvider. Values starting with $ are resolved from host environment variables.",
    )
    vercel_team_id: str | None = Field(
        default=None,
        description="Optional Vercel team id for VercelSandboxProvider. Values starting with $ are resolved from host environment variables.",
    )
    vercel_runtime: str | None = Field(
        default=None,
        description="Runtime passed to Vercel Sandbox (default: python3.13).",
    )
    vercel_image: str | None = Field(
        default=None,
        description="Optional custom image passed to Vercel Sandbox.",
    )
    vercel_timeout_ms: int | None = Field(
        default=None,
        gt=0,
        description="Vercel sandbox timeout in milliseconds (default: 3600000).",
    )
    vercel_vcpus: int | None = Field(
        default=None,
        gt=0,
        description="Vercel sandbox vCPU count (default: 2). Vercel accepts 1 or even values.",
    )
    vercel_memory_mb: int | None = Field(
        default=None,
        gt=0,
        description="Vercel sandbox memory in MiB. Must equal vercel_vcpus * 2048.",
    )
    vercel_ports: list[int] = Field(
        default_factory=list,
        description="Ports to expose from Vercel Sandbox for preview URLs.",
    )
    vercel_interactive: bool = Field(
        default=False,
        description="Whether to request an interactive Vercel sandbox session.",
    )
    vercel_environment: dict[str, str] = Field(
        default_factory=dict,
        description="Additional environment variables injected only into Vercel Sandbox. Values starting with $ are resolved from host environment variables.",
    )
    vercel_sync_max_file_bytes: int | None = Field(
        default=None,
        gt=0,
        description="Maximum file size copied between host thread data and Vercel Sandbox (default: 100 MiB).",
    )
    vercel_stop_on_release: bool = Field(
        default=True,
        description="Stop the persistent Vercel sandbox after each agent run so it snapshots and stops accruing idle runtime.",
    )
    vercel_record_store: str | None = Field(
        default=None,
        description="Mapping store for VercelSandboxProvider: `auto` uses the app database when available and file JSON otherwise; `database` requires database.backend sqlite/postgres; `file` keeps local JSON records for development.",
    )
    vercel_record_claim_timeout_s: float | None = Field(
        default=None,
        gt=0,
        description="Seconds to wait for another process to finish a DB-backed Vercel sandbox creation claim before failing acquire.",
    )

    bash_output_max_chars: int = Field(
        default=20000,
        ge=0,
        description="Maximum characters to keep from bash tool output. Output exceeding this limit is middle-truncated (head + tail), preserving the first and last half. Set to 0 to disable truncation.",
    )
    read_file_output_max_chars: int = Field(
        default=50000,
        ge=0,
        description="Maximum characters to keep from read_file tool output. Output exceeding this limit is head-truncated. Set to 0 to disable truncation.",
    )
    ls_output_max_chars: int = Field(
        default=20000,
        ge=0,
        description="Maximum characters to keep from ls tool output. Output exceeding this limit is head-truncated. Set to 0 to disable truncation.",
    )
    bash_command_timeout: int = Field(
        default=600,
        gt=0,
        description=(
            "Maximum wall-clock seconds a host bash command may run before it is terminated, process group and all (LocalSandboxProvider). "
            "Keeps a blocking foreground command (e.g. an un-backgrounded server) from hanging the turn; background `&` processes return immediately."
        ),
    )

    model_config = ConfigDict(extra="allow")
