"""Publishing viewer creations: a separate, trusted layer.

Nothing in here is reachable from generated code or from the model. Mika's
side can only ask for three narrow actions on a finished job:

    publish_project(job_id)
    announce_published_project(job_id)
    update_generated_games_section(job_id)

This package owns the GitHub and YouTube credentials, reads them from the
environment of the server process only, never logs them, and refuses to act
unless the matching feature flag is on and the job passed its checks.
"""

from .provenance import CreationJob, JobStore, safe_slug  # noqa: F401
from .service import PublicationService  # noqa: F401
from .settings import PublishSettings  # noqa: F401
