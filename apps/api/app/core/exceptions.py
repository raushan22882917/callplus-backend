class AppError(Exception):
    """Base class for domain errors mapped to HTTP responses in app.main."""

    status_code: int = 500
    detail: str = "Internal server error"

    def __init__(self, detail: str | None = None):
        self.detail = detail or self.detail
        super().__init__(self.detail)


class NotFoundError(AppError):
    status_code = 404
    detail = "Resource not found"


class ConflictError(AppError):
    status_code = 409
    detail = "Resource conflict"


class PermissionDeniedError(AppError):
    status_code = 403
    detail = "You do not have permission to perform this action"


class InvalidCredentialsError(AppError):
    status_code = 401
    detail = "Invalid email or password"


class TenantSelectionRequiredError(AppError):
    status_code = 409
    detail = "Multiple tenant memberships found; specify tenant_slug"
