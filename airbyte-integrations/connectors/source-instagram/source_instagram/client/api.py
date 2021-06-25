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


import urllib.parse as urlparse
from abc import ABC, abstractmethod
from typing import Any, Dict, Iterator, List, Optional, Sequence

import backoff
import pendulum
from facebook_business.adobjects.igmedia import IGMedia
from facebook_business.adobjects.iguser import IGUser
from facebook_business.api import Cursor
from facebook_business.exceptions import FacebookRequestError

from .common import retry_pattern

backoff_policy = retry_pattern(backoff.expo, FacebookRequestError, max_tries=7, factor=5)


def clear_url(record_data: dict = None):
    """
    This function removes the _nc_rid parameter from the video url and ccb from profile_picture_url for users.
    _nc_rid is generated every time a new one and ccb can change its value, and tests fail when checking for identity.
    This does not spoil the link, it remains correct and by clicking on it you can view the video or see picture.
    """

    def clear_query_params(url):
        parsed_url = urlparse.urlparse(url)
        res_query = []
        for q in parsed_url.query.split("&"):
            key, value = q.split("=")
            if key not in ["_nc_rid", "ccb"]:
                res_query.append(f"{key}={value}")

        parse_result = parsed_url._replace(query="&".join(res_query))
        return urlparse.urlunparse(parse_result)

    if record_data.get("media_type") == "VIDEO" and record_data.get("media_url"):
        record_data["media_url"] = clear_query_params(record_data["media_url"])
    elif record_data.get("profile_picture_url"):
        record_data["profile_picture_url"] = clear_query_params(record_data["profile_picture_url"])
    return record_data


class FBMarketingStream(Stream, ABC):
    """Base stream class"""

    primary_key = "id"

    page_size = 100

    enable_deleted = False
    entity_prefix = None

    def __init__(self, api: API, include_deleted: bool = False, **kwargs):
        super().__init__(**kwargs)
        self._api = api
        self._include_deleted = include_deleted if self.enable_deleted else False

    @cached_property
    def fields(self) -> List[str]:
        """List of fields that we want to query, for now just all properties from stream's schema"""
        return list(self.get_json_schema().get("properties", {}).keys())

    @backoff_policy
    def execute_in_batch(self, requests: Iterable[FacebookRequest]) -> Sequence[MutableMapping[str, Any]]:
        """Execute list of requests in batches"""
        records = []

        def success(response: FacebookResponse):
            records.append(response.json())

        def failure(response: FacebookResponse):
            raise response.error()

        api_batch: FacebookAdsApiBatch = self._api.api.new_batch()
        for request in requests:
            api_batch.add_request(request, success=success, failure=failure)
        retry_batch = api_batch.execute()
        if retry_batch:
            raise FacebookAPIException(f"Batch has failed {len(retry_batch)} requests")

        return records

    def read_records(
        self,
        sync_mode: SyncMode,
        cursor_field: List[str] = None,
        stream_slice: Mapping[str, Any] = None,
        stream_state: Mapping[str, Any] = None,
    ) -> Iterable[Mapping[str, Any]]:
        """Main read method used by CDK"""
        for record in self._read_records(params=self.request_params(stream_state=stream_state)):
            yield self._extend_record(record, fields=self.fields)

    def _read_records(self, params: Mapping[str, Any]) -> Iterable:
        """Wrapper around query to backoff errors.
        We have default implementation because we still can override read_records so this method is not mandatory.
        """
        return []

    @backoff_policy
    def _extend_record(self, obj: Any, **kwargs):
        """Wrapper around api_get to backoff errors"""
        return obj.api_get(**kwargs).export_all_data()

    def request_params(self, **kwargs) -> MutableMapping[str, Any]:
        """Parameters that should be passed to query_records method"""
        params = {"limit": self.page_size}

        if self._include_deleted:
            params.update(self._filter_all_statuses())

        return params

    def _filter_all_statuses(self) -> MutableMapping[str, Any]:
        """Filter that covers all possible statuses thus including deleted/archived records"""
        filt_values = [
            "active",
            "archived",
            "completed",
            "limited",
            "not_delivering",
            "deleted",
            "not_published",
            "pending_review",
            "permanently_deleted",
            "recently_completed",
            "recently_rejected",
            "rejected",
            "scheduled",
            "inactive",
        ]

        return {
            "filtering": [
                {"field": f"{self.entity_prefix}.delivery_info", "operator": "IN", "value": filt_values},
            ],
        }


class FBMarketingIncrementalStream(FBMarketingStream, ABC):
    cursor_field = "updated_time"


class InstagramStream(ABC):
    page_size = 100
    non_object_fields = ["page_id", "business_account_id"]

    def __init__(self, api, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._api = api

    @abstractmethod
    def list(self, fields: Sequence[str] = None) -> Iterator[dict]:
        """Iterate over entities"""

    def filter_input_fields(self, fields: Sequence[str] = None):
        return list(set(fields) - set(self.non_object_fields))

    @backoff_policy
    def load_next_page(self, instance: Cursor):
        instance.load_next_page()

    @backoff_policy
    def get_instance_cursor(self, ig_user: IGUser, method_name: str, params: dict = None, fields: Sequence[str] = None) -> Cursor:
        return getattr(ig_user, method_name)(params=params, fields=fields)

    def pagination(self, ig_user: IGUser, method_name: str, params: dict = None, fields: Sequence[str] = None) -> Iterator[Any]:
        """
        To implement pagination, we use private variables of the Cursor class.

        todo: Should be careful when updating the library version.
        """
        instance = self.get_instance_cursor(ig_user, method_name, params, fields)
        yield from instance._queue
        next_page = not instance._finished_iteration
        while next_page:
            self.load_next_page(instance)
            yield from instance._queue
            next_page = not instance._finished_iteration


class InstagramIncrementalStream(InstagramStream, ABC):
    @property
    @abstractmethod
    def state_pk(self):
        """Name of the field associated with the state"""

    @property
    @abstractmethod
    def cursor_field(self):
        """Name of the field associated with the account_id"""

    @property
    def state(self) -> Dict[str, str]:
        """
        State is a dictionary of the format {"account_id" : "cursor_value"}
        """

        return {account_id: str(account_state) for account_id, account_state in self._state.items()}

    @state.setter
    def state(self, value):
        # Convert State for each account from string to pendulum(datetime format)
        self._state = {account_id: pendulum.parse(account_state) for account_id, account_state in value.items()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._state = None

    def state_filter(self, record: dict) -> Optional[dict]:
        """Apply state filter to record, update cursor(state)"""

        cursor = pendulum.parse(record[self.state_pk])
        if self._state[record[self.cursor_field]] >= cursor:
            return

        stream_name = self.__class__.__name__
        if stream_name.endswith("API"):
            stream_name = stream_name[:-3]
        logger.info(
            f"Advancing bookmark for {stream_name} stream for {self.cursor_field} {record[self.cursor_field]} from {self._state[record[self.cursor_field]]} to {cursor}"
        )
        self._state.update({record[self.cursor_field]: max(cursor, self._state[record[self.cursor_field]])})
        return record


class Users(StreamAPI):
    def list(self, fields: Sequence[str] = None) -> Iterator[dict]:
        for account in self._api.accounts:
            yield {
                **{"page_id": account["page_id"]},
                **clear_url(self._extend_record(account["instagram_business_account"], fields=self.filter_input_fields(fields))),
            }

    @backoff_policy
    def _extend_record(self, ig_user: IGUser, fields: Sequence[str] = None) -> Dict:
        return ig_user.api_get(fields=fields).export_all_data()


class UserLifetimeInsights(StreamAPI):
    LIFETIME_METRICS = ["audience_city", "audience_country", "audience_gender_age", "audience_locale"]
    period = "lifetime"

    def list(self, fields: Sequence[str] = None) -> Iterator[dict]:
        for account in self._api.accounts:
            for insight in self._get_insight_records(account["instagram_business_account"], params=self._params()):
                yield {
                    "page_id": account["page_id"],
                    "business_account_id": account["instagram_business_account"].get("id"),
                    "metric": insight.get("name"),
                    "date": insight.get("values")[0]["end_time"],
                    "value": insight.get("values")[0]["value"],
                }

    def _params(self) -> Dict:
        return {"metric": self.LIFETIME_METRICS, "period": self.period}

    @backoff_policy
    def _get_insight_records(self, instagram_user: IGUser, params: dict = None) -> Iterator[Any]:
        return instagram_user.get_insights(params=params)


class UserInsights(IncrementalStreamAPI):
    METRICS_BY_PERIOD = {
        "day": [
            "email_contacts",
            "follower_count",
            "get_directions_clicks",
            "impressions",
            "phone_call_clicks",
            "profile_views",
            "reach",
            "text_message_clicks",
            "website_clicks",
        ],
        "week": ["impressions", "reach"],
        "days_28": ["impressions", "reach"],
        "lifetime": ["online_followers"],
    }

    state_pk = "date"
    cursor_field = "business_account_id"

    # We can only get User Insights data for today and the previous 29 days.
    # This is Facebook policy
    buffer_days = 29
    days_increment = 1

    def __init__(self, api):
        super().__init__(api=api)
        self._state = {}
        self._end_date = pendulum.now()

    def list(self, fields: Sequence[str] = None) -> Iterator[dict]:
        for account in self._api.accounts:
            account_id = account["instagram_business_account"].get("id")
            self._set_state(account_id)
            for params_per_day in self._params(account_id):
                insight_list = []
                for params in params_per_day:
                    insight_list += self._get_insight_records(account["instagram_business_account"], params=params)
                if not insight_list:
                    continue

                insight_record = {"page_id": account["page_id"], "business_account_id": account_id}
                for insight in insight_list:
                    key = (
                        f"{insight.get('name')}_{insight.get('period')}"
                        if insight.get("period") in ["week", "days_28"]
                        else insight.get("name")
                    )
                    insight_record[key] = insight.get("values")[0]["value"]
                    if not insight_record.get("date"):
                        insight_record["date"] = insight.get("values")[0]["end_time"]

                record = self.state_filter(insight_record)
                if record:
                    yield record

    def _params(self, account_id: str) -> Iterator[List]:
        buffered_start_date = self._state[account_id]

        while buffered_start_date <= self._end_date:
            params_list = []
            for period, metrics in self.METRICS_BY_PERIOD.items():
                params_list.append(
                    {
                        "metric": metrics,
                        "period": [period],
                        "since": buffered_start_date.to_datetime_string(),
                        "until": buffered_start_date.add(days=self.days_increment).to_datetime_string(),
                    }
                )
            yield params_list
            buffered_start_date = buffered_start_date.add(days=self.days_increment)

    def _set_state(self, account_id: str):
        start_date = self._state[account_id] if self._state.get(account_id) else self._api._start_date
        self._state[account_id] = max(start_date, pendulum.now().subtract(days=self.buffer_days))

    @backoff_policy
    def _get_insight_records(self, instagram_user: IGUser, params: dict = None) -> List:
        return instagram_user.get_insights(params=params)._queue


class Media(StreamAPI):
    # Children objects can only be of the media_type == "CAROUSEL_ALBUM".
    # And children object does not support INVALID_CHILDREN_FIELDS fields, so they are excluded when trying to get child objects to avoid the error.
    INVALID_CHILDREN_FIELDS = ["caption", "comments_count", "is_comment_enabled", "like_count", "children"]

    def list(self, fields: Sequence[str] = None) -> Iterator[dict]:
        children_fields = self.filter_input_fields(list(set(fields) - set(self.INVALID_CHILDREN_FIELDS)))
        for account in self._api.accounts:
            media = self._get_media(
                account["instagram_business_account"], {"limit": self.result_return_limit}, self.filter_input_fields(fields)
            )
            for record in media:
                record_data = record.export_all_data()
                if record_data.get("children"):
                    record_data["children"] = [
                        clear_url(self._get_single_record(child_record["id"], children_fields).export_all_data())
                        for child_record in record.get("children")["data"]
                    ]
                record_data.update(
                    {
                        "page_id": account["page_id"],
                        "business_account_id": account["instagram_business_account"].get("id"),
                    }
                )
                yield clear_url(record_data)

    def _get_media(self, instagram_user: IGUser, params: dict = None, fields: Sequence[str] = None) -> Iterator[Any]:
        yield from self.pagination(instagram_user, "get_media", params=params, fields=fields)

    @backoff_policy
    def _get_single_record(self, media_id: str, fields: Sequence[str] = None) -> IGMedia:
        return IGMedia(media_id).api_get(fields=fields)


class Stories(StreamAPI):
    def list(self, fields: Sequence[str] = None) -> Iterator[dict]:
        for account in self._api.accounts:
            stories = self._get_stories(
                account["instagram_business_account"], {"limit": self.result_return_limit}, self.filter_input_fields(fields)
            )
            for record in stories:
                record_data = record.export_all_data()
                record_data.update(
                    {
                        "page_id": account["page_id"],
                        "business_account_id": account["instagram_business_account"].get("id"),
                    }
                )
                yield clear_url(record_data)

    def _get_stories(self, instagram_user: IGUser, params: dict, fields: Sequence[str] = None) -> Iterator[Any]:
        yield from self.pagination(instagram_user, "get_stories", params=params, fields=fields)


class MediaInsights(MediaAPI):
    MEDIA_METRICS = ["engagement", "impressions", "reach", "saved"]
    CAROUSEL_ALBUM_METRICS = ["carousel_album_engagement", "carousel_album_impressions", "carousel_album_reach", "carousel_album_saved"]

    def list(self, fields: Sequence[str] = None) -> Iterator[dict]:
        for account in self._api.accounts:
            ig_account = account["instagram_business_account"]
            media = self._get_media(ig_account, {"limit": self.result_return_limit}, ["media_type"])
            for ig_media in media:
                account_id = ig_account.get("id")
                media_insights = self._get_insights(ig_media, account_id)
                if media_insights is None:
                    break
                yield {
                    **{
                        "id": ig_media.get("id"),
                        "page_id": account["page_id"],
                        "business_account_id": account_id,
                    },
                    **{record.get("name"): record.get("values")[0]["value"] for record in media_insights},
                }

    @backoff_policy
    def _get_insights(self, item, account_id) -> Optional[Iterator[Any]]:
        """
        This is necessary because the functions that call this endpoint return
        a generator, whose calls need decorated with a backoff.
        """
        if item.get("media_type") == "VIDEO":
            metrics = self.MEDIA_METRICS + ["video_views"]
        elif item.get("media_type") == "CAROUSEL_ALBUM":
            metrics = self.CAROUSEL_ALBUM_METRICS
        else:
            metrics = self.MEDIA_METRICS

        try:
            return item.get_insights(params={"metric": metrics})
        except FacebookRequestError as error:
            # An error might occur if the media was posted before the most recent time that
            # the user's account was converted to a business account from a personal account
            if error.api_error_subcode() == 2108006:
                logger.error(f"Insights error for business_account_id {account_id}: {error.body()}")

                # We receive all Media starting from the last one, and if on the next Media we get an Insight error,
                # then no reason to make inquiries for each Media further, since they were published even earlier.
                return None
            raise error


class StoriesInsights(Stories):
    STORY_METRICS = ["exits", "impressions", "reach", "replies", "taps_forward", "taps_back"]

    def list(self, fields: Sequence[str] = None) -> Iterator[dict]:
        for account in self._api.accounts:
            stories = self._get_stories(account["instagram_business_account"], {"limit": self.result_return_limit}, fields=[])
            for ig_story in stories:
                insights = self._get_insights(ig_story)
                if insights:
                    yield {
                        **{
                            "id": ig_story.get("id"),
                            "page_id": account["page_id"],
                            "business_account_id": account["instagram_business_account"].get("id"),
                        },
                        **{record.get("name"): record.get("values")[0]["value"] for record in insights},
                    }

    @backoff_policy
    def _get_insights(self, item) -> Iterator[Any]:
        """
        This is necessary because the functions that call this endpoint return
        a generator, whose calls need decorated with a backoff.
        """

        # Story IG Media object metrics with values less than 5 will return an error code 10 with the message (#10)
        # Not enough viewers for the media to show insights.
        try:
            return item.get_insights(params={"metric": self.STORY_METRICS})
        except FacebookRequestError as error:
            logger.error(f"Insights error: {error.api_error_message()}")
            if error.api_error_code() == 10:
                return []
            raise error
