"""
This module provides tools to call moto using moto and botocore internals without going through the moto HTTP server.
"""
import sys
from functools import lru_cache
from typing import Callable

import moto.backends as moto_backends
from moto.core.exceptions import RESTError
from moto.core.utils import BackendDict
from moto.moto_server.utilities import RegexConverter
from werkzeug.routing import Map, Rule

from localstack import __version__ as localstack_version
from localstack import config
from localstack.aws.accounts import get_aws_account_id
from localstack.aws.api import (
    CommonServiceException,
    HttpRequest,
    HttpResponse,
    RequestContext,
    ServiceRequest,
    ServiceResponse,
)
from localstack.aws.client import parse_response
from localstack.aws.forwarder import (
    ForwardingFallbackDispatcher,
    HttpBackendResponse,
    create_aws_request_context,
)
from localstack.aws.skeleton import DispatchTable
from localstack.http import Response
from localstack.constants import DEFAULT_AWS_ACCOUNT_ID

MotoResponse = HttpBackendResponse
MotoDispatcher = Callable[[HttpRequest, str, dict], MotoResponse]

user_agent = f"Localstack/{localstack_version} Python/{sys.version.split(' ')[0]}"


def call_moto(context: RequestContext, include_response_metadata=False) -> ServiceResponse:
    """
    Call moto with the given request context and receive a parsed ServiceResponse.

    :param context: the request context
    :param include_response_metadata: whether to include botocore's "ResponseMetadata" attribute
    :return: a serialized AWS ServiceResponse (same as boto3 would return)
    """
    status, headers, content = dispatch_to_moto(context)

    response = parse_response(context.operation, Response(content, status, headers))

    if status >= 301:
        error = response["Error"]
        raise CommonServiceException(
            code=error.get("Code", "UnknownError"),
            status_code=status,
            message=error.get("Message", ""),
        )

    if not include_response_metadata:
        response.pop("ResponseMetadata", None)

    return response


def call_moto_with_request(
    context: RequestContext, service_request: ServiceRequest
) -> ServiceResponse:
    """
    Like `call_moto`, but you can pass a modified version of the service request before calling moto. The caveat is
    that a new HTTP request has to be created. The service_request is serialized into a new RequestContext object,
    and headers from the old request are merged into the new one.

    :param context: the original request context
    :param service_request: the dictionary containing the service request parameters
    :return: a serialized AWS ServiceResponse (same as boto3 would return)
    """
    local_context = create_aws_request_context(
        service_name=context.service.service_name,
        action=context.operation.name,
        parameters=service_request,
        region=context.region,
    )

    local_context.request.headers.extend(context.request.headers)

    return call_moto(local_context)


def proxy_moto(context: RequestContext, service_request: ServiceRequest = None) -> HttpResponse:
    """
    Similar to ``call``, only that ``proxy`` does not parse the HTTP response into a ServiceResponse, but instead
    returns directly the HTTP response. This can be useful to pass through moto's response directly to the client.

    :param context: the request context
    :param service_request: currently not being used, added to satisfy ServiceRequestHandler contract
    :return: the HttpResponse from moto
    """
    status, headers, content = dispatch_to_moto(context)

    return HttpResponse(response=content, status=status, headers=headers)


def MotoFallbackDispatcher(provider: object) -> DispatchTable:
    """
    Wraps a provider with a moto fallthrough mechanism. It does by creating a new DispatchTable from the original
    provider, and wrapping each method with a fallthrough method that calls ``request`` if the original provider
    raises a ``NotImplementedError``.

    :param provider: the ASF provider
    :return: a modified DispatchTable
    """
    return ForwardingFallbackDispatcher(provider, proxy_moto)


def dispatch_to_moto(context: RequestContext) -> MotoResponse:
    """
    Internal method to dispatch the request to moto without changing moto's dispatcher output.
    :param context: the request context
    :return: the response from moto
    """
    service = context.service
    request = context.request

    # this is where we skip the HTTP roundtrip between the moto server and the boto client
    dispatch = get_dispatcher(service.service_name, request.path)

    # TODO@viren temporory hack. This is to be refactored when account ID no longer lives in global TLS
    request.headers.add("x-moto-account-id", get_aws_account_id())

    try:
        return dispatch(request, request.url, request.headers)
    except RESTError as e:
        raise CommonServiceException(e.error_type, e.message, status_code=e.code) from e


def get_dispatcher(service: str, path: str) -> MotoDispatcher:
    url_map = get_moto_routing_table(service)

    if len(url_map._rules) == 1:
        # in most cases, there will only be one dispatch method in the list of urls, so no need to do matching
        rule = next(url_map.iter_rules())
        return rule.endpoint

    matcher = url_map.bind(config.LOCALSTACK_HOSTNAME)
    endpoint, _ = matcher.match(path_info=path)
    return endpoint


@lru_cache()
def get_moto_routing_table(service: str) -> Map:
    """Cached version of load_moto_routing_table."""
    return load_moto_routing_table(service)


def load_moto_routing_table(service: str) -> Map:
    """
    Creates from moto service url_paths a werkzeug URL rule map that can be used to locate moto methods to dispatch
    requests to.

    :param service: the service to get the map for.
    :return: a new Map object
    """
    # code from moto.moto_server.werkzeug_app.create_backend_app
    backend_dict = moto_backends.get_backend(service)
    # Get an instance of this backend.
    # We'll only use this backend to resolve the URL's, so the exact region/account_id is irrelevant
    if isinstance(backend_dict, BackendDict):
        if "us-east-1" in backend_dict[DEFAULT_AWS_ACCOUNT_ID]:
            backend = backend_dict[DEFAULT_AWS_ACCOUNT_ID]["us-east-1"]
        else:
            backend = backend_dict[DEFAULT_AWS_ACCOUNT_ID]["global"]
    else:
        backend = backend_dict["global"]

    url_map = Map()
    url_map.converters["regex"] = RegexConverter

    for url_path, handler in backend.flask_paths.items():
        # Some URL patterns in moto have optional trailing slashes, for example the route53 pattern:
        # r"{0}/(?P<api_version>[\d_-]+)/hostedzone/(?P<zone_id>[^/]+)/rrset/?$".
        # However, they don't actually seem to work. Routing only works because moto disables strict_slashes check
        # for the URL Map. So we also disable it here explicitly.
        strict_slashes = False

        # Rule endpoints are annotated as string types in werkzeug, but they don't have to be.
        endpoint = handler

        url_map.add(Rule(url_path, endpoint=endpoint, strict_slashes=strict_slashes))

    return url_map
