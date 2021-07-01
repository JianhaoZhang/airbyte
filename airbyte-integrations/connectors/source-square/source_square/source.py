#
# MIT License
#
# Copyright (c) 2020 Airbyte
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#

import json
from abc import ABC
from typing import Any, Iterable, List, Mapping, MutableMapping, Optional, Tuple

import pendulum
import requests
from airbyte_cdk.models import SyncMode
from airbyte_cdk.sources import AbstractSource
from airbyte_cdk.sources.streams import Stream
from airbyte_cdk.sources.streams.http import HttpStream
from airbyte_cdk.sources.streams.http.auth import TokenAuthenticator


class SquareStream(HttpStream, ABC):
    def __init__(self, is_sandbox: bool, api_version: str, start_date: str, include_deleted_objects: bool, **kwargs):
        super().__init__(**kwargs)
        self.is_sandbox = is_sandbox
        self.api_version = api_version
        # Converting users ISO 8601 format (YYYY-MM-DD) to RFC 3339 (2021-06-14T13:47:56.799Z)
        # Because this standard is used by square in 'updated_at' records field
        self.start_date = pendulum.parse(start_date).to_rfc3339_string()
        self.include_deleted_objects = include_deleted_objects

    data_field = None
    primary_key = "id"
    items_per_page_limit = 100

    @property
    def url_base(self) -> str:
        return "https://connect.squareup{}.com/v2/".format("sandbox" if self.is_sandbox else "")

    def next_page_token(self, response: requests.Response) -> Optional[Mapping[str, Any]]:
        next_page_cursor = response.json().get("cursor", False)
        if next_page_cursor:
            return {"cursor": next_page_cursor}

    def request_headers(
        self, stream_state: Mapping[str, Any], stream_slice: Mapping[str, Any] = None, next_page_token: Mapping[str, Any] = None
    ) -> Mapping[str, Any]:
        return {"Square-Version": self.api_version, "Content-Type": "application/json"}

    def parse_response(self, response: requests.Response, **kwargs) -> Iterable[Mapping]:
        json_response = response.json()
        records = json_response.get(self.data_field, []) if self.data_field is not None else json_response
        yield from records

    def _send_request(self, request: requests.PreparedRequest) -> requests.Response:
        try:
            return super()._send_request(request)
        except requests.exceptions.HTTPError as e:
            if e.response.content:
                content = json.loads(e.response.content.decode())
                if content and "errors" in content:
                    raise SquareException(str(content["errors"]))
            else:
                raise e


class SquareException(Exception):
    """ Just for formatting the exception as Square"""


class SquareCatalogObjectsStream(SquareStream):
    data_field = "objects"
    http_method = "POST"
    items_per_page_limit = 1000

    def path(
        self, stream_state: Mapping[str, Any] = None, stream_slice: Mapping[str, Any] = None, next_page_token: Mapping[str, Any] = None
    ) -> str:
        return "catalog/search"

    def request_body_json(
        self, stream_state: Mapping[str, Any], stream_slice: Mapping[str, Any] = None, next_page_token: Mapping[str, Any] = None
    ) -> Optional[Mapping]:

        json_payload = super().request_body_json(stream_state, stream_slice, next_page_token)

        if not json_payload:
            json_payload = {}

        if self.path() == "catalog/search":
            json_payload.update(
                {
                    "include_deleted_objects": self.include_deleted_objects,
                    "include_related_objects": False,
                    "limit": self.items_per_page_limit,
                }
            )

        if next_page_token:
            json_payload.update({"cursor": next_page_token["cursor"]})

        return json_payload


class IncrementalSquareGenericStream(SquareStream, ABC):
    def get_updated_state(self, current_stream_state: MutableMapping[str, Any], latest_record: Mapping[str, Any]) -> Mapping[str, Any]:

        if current_stream_state is not None and self.cursor_field in current_stream_state:
            return {self.cursor_field: max(current_stream_state[self.cursor_field], latest_record[self.cursor_field])}
        else:
            return {self.cursor_field: self.start_date}


class IncrementalSquareCatalogObjectsStream(SquareCatalogObjectsStream, IncrementalSquareGenericStream, ABC):
    state_checkpoint_interval = SquareCatalogObjectsStream.items_per_page_limit

    cursor_field = "updated_at"

    def request_body_json(
        self, stream_state: Mapping[str, Any], stream_slice: Mapping[str, Any] = None, next_page_token: Mapping[str, Any] = None
    ) -> Optional[Mapping]:
        json_payload = super().request_body_json(stream_state, stream_slice, next_page_token)

        if self.cursor_field in stream_state:
            json_payload.update({"begin_time": stream_state[self.cursor_field]})

        return json_payload


class IncrementalSquareStream(IncrementalSquareGenericStream, ABC):
    state_checkpoint_interval = SquareStream.items_per_page_limit

    cursor_field = "created_at"

    def request_params(
        self,
        stream_state: Mapping[str, Any],
        stream_slice: Mapping[str, Any] = None,
        next_page_token: Mapping[str, Any] = None,
    ) -> MutableMapping[str, Any]:

        params_payload = super().request_params(stream_state, stream_slice, next_page_token)
        params_payload = params_payload if params_payload else {}

        if self.cursor_field in stream_state:
            params_payload.update({"begin_time": stream_state[self.cursor_field]})

        if next_page_token:
            return params_payload.update({"cursor": next_page_token["cursor"]})

        params_payload.update({"limit": self.items_per_page_limit})

        return params_payload


class Items(IncrementalSquareCatalogObjectsStream):
    """Docs: https://developer.squareup.com/explorer/square/catalog-api/search-catalog-objects
    with object_types = ITEM"""

    def request_body_json(self, **kwargs) -> Optional[Mapping]:
        return {**super().request_body_json(**kwargs), "object_types": ["ITEM"]}


class Categories(IncrementalSquareCatalogObjectsStream):
    """Docs: https://developer.squareup.com/explorer/square/catalog-api/search-catalog-objects
    with object_types = CATEGORY"""

    def request_body_json(self, **kwargs) -> Optional[Mapping]:
        return {**super().request_body_json(**kwargs), "object_types": ["CATEGORY"]}


class Discounts(IncrementalSquareCatalogObjectsStream):
    """Docs: https://developer.squareup.com/explorer/square/catalog-api/search-catalog-objects
    with object_types = DISCOUNT"""

    def request_body_json(self, **kwargs) -> Optional[Mapping]:
        return {**super().request_body_json(**kwargs), "object_types": ["DISCOUNT"]}


class Taxes(IncrementalSquareCatalogObjectsStream):
    """Docs: https://developer.squareup.com/explorer/square/catalog-api/search-catalog-objects
    with object_types = TAX"""

    def request_body_json(self, **kwargs) -> Optional[Mapping]:
        return {**super().request_body_json(**kwargs), "object_types": ["TAX"]}


class ModifierList(IncrementalSquareCatalogObjectsStream):
    """Docs: https://developer.squareup.com/explorer/square/catalog-api/search-catalog-objects
    with object_types = MODIFIER_LIST"""

    def request_body_json(self, **kwargs) -> Optional[Mapping]:
        return {**super().request_body_json(**kwargs), "object_types": ["MODIFIER_LIST"]}


class Refunds(IncrementalSquareStream):
    """ Docs: https://developer.squareup.com/reference/square_2021-06-16/refunds-api/list-payment-refunds """

    data_field = "refunds"

    def path(self, **kwargs) -> str:
        return "refunds"

    def request_params(self, **kwargs) -> MutableMapping[str, Any]:
        params_payload = super().request_params(**kwargs)
        return {**params_payload, "sort_order": "ASC"} if params_payload else {"sort_order": "ASC"}


class Payments(IncrementalSquareStream):
    """ Docs: https://developer.squareup.com/reference/square_2021-06-16/payments-api/list-payments """

    data_field = "payments"

    def path(self, **kwargs) -> str:
        return "payments"

    def request_params(self, **kwargs) -> MutableMapping[str, Any]:
        params_payload = super().request_params(**kwargs)
        return {**params_payload, "sort_order": "ASC"} if params_payload else {"sort_order": "ASC"}


class Locations(SquareStream):
    """ Docs: https://developer.squareup.com/explorer/square/locations-api/list-locations """

    data_field = "locations"

    def path(
        self, stream_state: Mapping[str, Any] = None, stream_slice: Mapping[str, Any] = None, next_page_token: Mapping[str, Any] = None
    ) -> str:
        return "locations"


class Shifts(SquareStream):
    """ Docs: https://developer.squareup.com/reference/square/labor-api/search-shifts """

    data_field = "shifts"
    http_method = "POST"
    items_per_page_limit = 200

    def path(
        self, stream_state: Mapping[str, Any] = None, stream_slice: Mapping[str, Any] = None, next_page_token: Mapping[str, Any] = None
    ) -> str:
        return "labor/shifts/search"

    def request_body_json(self, **kwargs) -> Optional[Mapping]:
        json_payload = super().request_body_json(**kwargs)
        if not json_payload:
            json_payload = {}

        if "next_page_token" in kwargs and kwargs["next_page_token"]:
            json_payload.update({"cursor": kwargs["next_page_token"]["cursor"]})

        return json_payload


class TeamMembers(SquareStream):
    """ Docs: https://developer.squareup.com/reference/square/team-api/search-team-members """

    data_field = "team_members"
    http_method = "POST"

    def path(
        self, stream_state: Mapping[str, Any] = None, stream_slice: Mapping[str, Any] = None, next_page_token: Mapping[str, Any] = None
    ) -> str:
        return "team-members/search"

    def request_body_json(self, **kwargs) -> Optional[Mapping]:
        json_payload = super().request_body_json(**kwargs)
        if not json_payload:
            json_payload = {}

        if "next_page_token" in kwargs and kwargs["next_page_token"]:
            json_payload.update({"cursor": kwargs["next_page_token"]["cursor"]})

        json_payload.update({"limit": self.items_per_page_limit})
        return json_payload


class TeamMemberWages(SquareStream):
    """ Docs: https://developer.squareup.com/reference/square_2021-06-16/labor-api/list-team-member-wages """

    data_field = "team_member_wages"
    items_per_page_limit = 200

    def path(
        self, stream_state: Mapping[str, Any] = None, stream_slice: Mapping[str, Any] = None, next_page_token: Mapping[str, Any] = None
    ) -> str:
        return "labor/team-member-wages"

    def request_params(self, **kwargs) -> MutableMapping[str, Any]:
        params_payload = super().request_params(**kwargs)
        if not params_payload:
            params_payload = {}

        if "next_page_token" in kwargs and kwargs["next_page_token"]:
            params_payload.update({"cursor": kwargs["next_page_token"]["cursor"]})

        params_payload.update({"limit": self.items_per_page_limit})
        return params_payload


class Customers(SquareStream):
    """ Docs: https://developer.squareup.com/reference/square_2021-06-16/customers-api/list-customers """

    data_field = "customers"

    def path(self, **kwargs) -> str:
        return "customers"

    def request_params(self, **kwargs) -> MutableMapping[str, Any]:
        params_payload = super().request_params(**kwargs)
        if not params_payload:
            params_payload = {}

        if "next_page_token" in kwargs and kwargs["next_page_token"]:
            params_payload.update({"cursor": kwargs["next_page_token"]["cursor"]})

        params_payload.update({"sort_order": "ASC", "sort_field": "CREATED_AT"})
        return params_payload


class Orders(SquareStream):
    """ Docs: https://developer.squareup.com/reference/square/orders-api/search-orders """

    data_field = "orders"
    http_method = "POST"
    items_per_page_limit = 500

    def path(self, **kwargs) -> str:
        return "orders/search"

    def request_body_json(self, **kwargs) -> Optional[Mapping]:
        json_payload = super().request_body_json(**kwargs)
        if not json_payload:
            json_payload = {}

        args = {
            "authenticator": self.authenticator,
            "is_sandbox": self.is_sandbox,
            "api_version": self.api_version,
            "start_date": self.start_date,
            "include_deleted_objects": self.include_deleted_objects,
        }

        location_ids = []
        for location_item in Locations(**args).read_records(sync_mode=SyncMode.full_refresh):
            location_ids.append(location_item["id"])

        if location_ids:
            json_payload.update({"location_ids": location_ids})

        if "next_page_token" in kwargs and kwargs["next_page_token"]:
            json_payload.update({"cursor": kwargs["next_page_token"]["cursor"]})

        json_payload.update({"limit": self.items_per_page_limit})
        return json_payload


class SourceSquare(AbstractSource):
    api_version = "2021-06-16"  # Latest Stable Release

    def check_connection(self, logger, config) -> Tuple[bool, any]:

        headers = {
            "Square-Version": self.api_version,
            "Authorization": "Bearer {}".format(config["api_key"]),
            "Content-Type": "application/json",
        }
        url = "https://connect.squareup{}.com/v2/catalog/info".format("sandbox" if config["is_sandbox"] else "")

        try:
            session = requests.get(url, headers=headers)
            session.raise_for_status()
            return True, None
        except requests.exceptions.RequestException as e:
            if e.response.status_code == 401:
                return False, "Unauthorized. Check your credentials"

            return False, e

    def streams(self, config: Mapping[str, Any]) -> List[Stream]:

        auth = TokenAuthenticator(token=config["api_key"])
        args = {
            "authenticator": auth,
            "is_sandbox": config["is_sandbox"],
            "api_version": self.api_version,
            "start_date": config["start_date"],
            "include_deleted_objects": config["include_deleted_objects"],
        }
        return [
            Items(**args),
            Categories(**args),
            Discounts(**args),
            Taxes(**args),
            Locations(**args),
            TeamMembers(**args),
            TeamMemberWages(**args),
            Refunds(**args),
            Payments(**args),
            Customers(**args),
            ModifierList(**args),
            Shifts(**args),
            Orders(**args),
        ]
