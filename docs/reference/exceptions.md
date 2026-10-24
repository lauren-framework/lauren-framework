# Exception Catalog

Every typed exception class exported by the framework. The generated table of
statuses and codes lives in the [Error Catalog](errors.md); `error_catalog()`
returns it as data.

## Base classes

::: lauren.LaurenError

::: lauren.StartupError

::: lauren.HTTPError

::: lauren.LifecycleError

## Startup errors

::: lauren.RouterConflictError

::: lauren.CircularDependencyError

::: lauren.CircularModuleError

::: lauren.DecoratorUsageError

::: lauren.MissingProviderError

::: lauren.ProtocolAmbiguityError

::: lauren.ModuleExportViolation

::: lauren.LifecycleConfigError

::: lauren.MetadataInheritanceError

::: lauren.DuplicateBindingError

::: lauren.UnresolvableParameterError

::: lauren.DIScopeViolationError

::: lauren.MiddlewareConfigError

::: lauren.GuardConfigError

::: lauren.InterceptorConfigError

::: lauren.ExceptionHandlerConfigError

::: lauren.SessionConfigError

::: lauren.OpenAPISchemaError

## HTTP errors

::: lauren.ExtractorError

::: lauren.ExtractorFieldError

::: lauren.BadRequestError

::: lauren.UnauthorizedError

::: lauren.ForbiddenError

::: lauren.NotFoundError

::: lauren.RouteNotFoundError

::: lauren.MethodNotAllowedError

::: lauren.NotAcceptableError

::: lauren.ConflictError

::: lauren.GoneError

::: lauren.PreconditionFailedError

::: lauren.RequestBodyTooLarge

::: lauren.UnsupportedMediaTypeError

::: lauren.TooManyRequestsError

::: lauren.ServiceUnavailableError

::: lauren.RequestTimeoutError

::: lauren.MissingStateError

::: lauren.StateTypeError

## Lifecycle errors

::: lauren.LifecycleViolationError

::: lauren.DestructError

::: lauren.DestructTimeoutError

::: lauren.DrainTimeoutError
