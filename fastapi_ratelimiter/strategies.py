import abc
import hashlib
import inspect
import time
import zlib
from dataclasses import dataclass
from typing import Sequence, Union, Callable, Optional, Awaitable

from aioredis.client import Pipeline, Redis
from starlette.requests import Request

from fastapi_ratelimiter.config import RateLimitConfig
from fastapi_ratelimiter.utils import extract_ip_from_request

DEFAULT_PREFIX = "rl:"

# Extend the expiration time by a few seconds to avoid misses.
EXPIRATION_FUDGE = 5
RequestIdentifierFactoryType = Callable[[Request], Union[str, bytes, Awaitable[Union[str, bytes]]]]


@dataclass
class RateLimitStatus:
    number_of_requests: int
    ratelimit_config: RateLimitConfig
    time_left: int

    @property
    def remaining_number_of_requests(self) -> int:
        return self.limit - self.number_of_requests

    @property
    def limit(self) -> int:
        return self.ratelimit_config.max_count

    @property
    def should_limit(self) -> bool:
        return self.number_of_requests > self.limit


class AbstractRateLimitStrategy(abc.ABC):

    def __init__(
            self,
            rate: str,
            prefix: str = DEFAULT_PREFIX,
            request_identifier_factory: Optional[RequestIdentifierFactoryType] = None
    ):
        if request_identifier_factory is None:
            request_identifier_factory = extract_ip_from_request
        self._request_identifier = request_identifier_factory
        self._ratelimit_config = RateLimitConfig.from_string(rate)
        self._prefix = prefix

    @abc.abstractmethod
    async def get_ratelimit_status(self, request: Request) -> RateLimitStatus:
        pass

    async def _get_request_identifier(self, request: Request) -> Union[str, bytes]:
        if (
                inspect.iscoroutine(self._request_identifier)
                or inspect.iscoroutinefunction(self._request_identifier)
        ):
            return await self._response_on_limit_exceeded(request)  # type: ignore

        return self._request_identifier(request)


class BucketingRateLimitStrategy(AbstractRateLimitStrategy):

    async def get_ratelimit_status(self, request: Request) -> RateLimitStatus:
        request_identifier = await self._get_request_identifier(request)
        window = self._get_window(request_identifier)
        storage_key = self._create_storage_key(request, request_identifier, str(window))

        redis: Redis = request.state.redis
        async with redis.pipeline() as pipe:  # type: Pipeline
            pipeline_result: Sequence[int, bool] = await (
                pipe.incr(storage_key).expire(
                    storage_key,
                    self._ratelimit_config.period_in_seconds + EXPIRATION_FUDGE
                ).execute()
            )

        number_of_requests = pipeline_result[0]
        return RateLimitStatus(
            number_of_requests=number_of_requests,
            ratelimit_config=self._ratelimit_config,
            time_left=window - int(time.time())
        )

    def _create_storage_key(self, *parts) -> str:
        safe_rate = '%d/%ds' % (self._ratelimit_config.max_count, self._ratelimit_config.period_in_seconds)
        return self._prefix + hashlib.md5(u''.join((safe_rate, *parts)).encode('utf-8')).hexdigest()

    async def _get_window(self, request_identifier: Union[str, bytes]) -> int:
        """
        Given a request identifier, and time period return when the end of the current time
        period for rate evaluation is.
        """
        period = self._ratelimit_config.period_in_seconds
        epoch_time = int(time.time())
        if period == 1:
            return epoch_time
        if not isinstance(request_identifier, bytes):
            request_identifier = request_identifier.encode('utf-8')
        # This logic determines either the last or current end of a time period.
        # Subtracting (epoch_time % period) gives us the a consistent edge from the epoch.
        # We use (zlib.crc32(value) % period) to add a consistent jitter so that
        # all time periods don't end at the same time.
        w = epoch_time - (epoch_time % period) + (zlib.crc32(request_identifier) % period)
        if w < epoch_time:
            return w + period
        return w


class SlidingWindowLimitStrategy(AbstractRateLimitStrategy):
    async def get_ratelimit_status(self, request: Request) -> RateLimitStatus:
        request_identifier = await self._get_request_identifier(request)
        storage_key = f"{self._prefix}:{request_identifier}"

        epoch_ms = int(time.time() * 1000)
        period_in_seconds = self._ratelimit_config.period_in_seconds

        redis: Redis = request.state.redis
        async with redis.pipeline() as pipe:  # type: Pipeline
            result = await (
                pipe.zremrangebyscore(
                    storage_key, 0, epoch_ms - (period_in_seconds * 100)
                ).zadd(
                    storage_key,
                    {
                        f"{epoch_ms}:1": epoch_ms
                    }
                ).zrange(
                    storage_key, 0, -1
                ).expire(
                    storage_key, (period_in_seconds * 1000) + 1
                ).execute()
            )

        number_of_requests = sum(int(i.split(':')[-1]) for i in result[2])
        return RateLimitStatus(
            number_of_requests=number_of_requests,
            ratelimit_config=self._ratelimit_config,
            time_left=-1
        )
