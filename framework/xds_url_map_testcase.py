# Copyright 2021 The gRPC Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""A test framework built for urlMap related xDS test cases."""

import abc
from dataclasses import dataclass
import datetime
import os
import sys
import time
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple
import unittest

from absl import flags
from absl import logging
import grpc

from framework import xds_k8s_testcase
from framework import xds_url_map_test_resources
from framework.helpers import grpc as helpers_grpc
from framework.helpers import retryers
from framework.helpers import skips
from framework.infrastructure import k8s
from framework.rpc import grpc_csds
from framework.rpc import grpc_testing
from framework.test_app import client_app
from framework.test_app.runners.k8s import k8s_xds_client_runner
from framework.test_cases import base_testcase

# Load existing flags
flags.adopt_module_key_flags(xds_k8s_testcase)
flags.adopt_module_key_flags(xds_url_map_test_resources)

# Define urlMap specific flags
QPS = flags.DEFINE_integer("qps", default=25, help="The QPS client is sending")

# Test configs
_URL_MAP_PROPAGATE_TIMEOUT_SEC = 600
# With the per-run IAM change, the first xDS response has a several minutes
# delay. We want to increase the interval, reduce the log spam.
_URL_MAP_PROPAGATE_CHECK_INTERVAL_SEC = 15
URL_MAP_TESTCASE_FILE_SUFFIX = "_test.py"
_CLIENT_CONFIGURE_WAIT_SEC = 2

# Type aliases
XdsTestClient = client_app.XdsTestClient
GcpResourceManager = xds_url_map_test_resources.GcpResourceManager
HostRule = xds_url_map_test_resources.HostRule
PathMatcher = xds_url_map_test_resources.PathMatcher
_KubernetesClientRunner = k8s_xds_client_runner.KubernetesClientRunner
_timedelta = datetime.timedelta


def _split_camel(s: str, delimiter: str = "-") -> str:
    """Turn camel case name to snake-case-like name."""
    return "".join(
        delimiter + c.lower() if c.isupper() else c for c in s
    ).lstrip(delimiter)


@dataclass
class ExpectedResult:
    """Describes the expected result of assertRpcStatusCode method below."""

    rpc_type: str = grpc_testing.RPC_TYPE_UNARY_CALL
    status_code: grpc.StatusCode = grpc.StatusCode.OK
    ratio: float = 1


class _MetaXdsUrlMapTestCase(type):
    """Tracking test case subclasses."""

    # Automatic discover of all subclasses
    _test_case_classes = []
    _test_case_names = set()
    # Keep track of started and finished test cases, so we know when to setup
    # and tear down GCP resources.
    _started_test_cases = set()
    _finished_test_cases = set()

    def __new__(
        cls, name: str, bases: Iterable[Any], attrs: Mapping[str, Any]
    ) -> Any:
        # Hand over the tracking objects
        attrs["test_case_classes"] = cls._test_case_classes
        attrs["test_case_names"] = cls._test_case_names
        attrs["started_test_cases"] = cls._started_test_cases
        attrs["finished_test_cases"] = cls._finished_test_cases
        # Handle the test name reflection
        module_name = os.path.split(sys.modules[attrs["__module__"]].__file__)[
            -1
        ]
        if module_name.endswith(URL_MAP_TESTCASE_FILE_SUFFIX):
            module_name = module_name.replace(URL_MAP_TESTCASE_FILE_SUFFIX, "")
        attrs["short_module_name"] = module_name.replace("_", "-")
        # Create the class and track
        new_class = type.__new__(cls, name, bases, attrs)
        if name.startswith("Test"):
            cls._test_case_names.add(name)
            cls._test_case_classes.append(new_class)
        else:
            logging.debug("Skipping test case class: %s", name)
        return new_class


class XdsUrlMapTestCase(
    base_testcase.BaseTestCase, metaclass=_MetaXdsUrlMapTestCase
):
    """XdsUrlMapTestCase is the base class for urlMap related tests.

    The subclass is expected to implement 3 methods:

    - url_map_change: Updates the urlMap components for this test case
    - xds_config_validate: Validates if the client received legit xDS configs
    - rpc_distribution_validate: Validates if the routing behavior is correct
    """

    test_client_runner: Optional[_KubernetesClientRunner] = None

    @staticmethod
    def is_supported(config: skips.TestConfig) -> bool:
        """Allow the test case to decide whether it supports the given config.

        Returns:
          A bool indicates if the given config is supported.
        """
        del config
        return True

    @staticmethod
    def client_init_config(rpc: str, metadata: str) -> Tuple[str, str]:
        """Updates the initial RPC configs for this test case.

        Each test case will start a test client. The client takes RPC configs
        and starts to send RPCs immediately. The config returned by this
        function will be used to replace the default configs.

        The default configs are passed in as arguments, so this method can
        modify part of them.

        Args:
            rpc: The default rpc config, specifying RPCs to send, format
            'UnaryCall,EmptyCall'
            metadata: The metadata config, specifying metadata to send with each
            RPC, format 'EmptyCall:key1:value1,UnaryCall:key2:value2'.

        Returns:
            A tuple contains the updated rpc and metadata config.
        """
        return rpc, metadata

    @staticmethod
    @abc.abstractmethod
    def url_map_change(
        host_rule: HostRule, path_matcher: PathMatcher
    ) -> Tuple[HostRule, PathMatcher]:
        """Updates the dedicated urlMap components for this test case.

        Each test case will have a dedicated HostRule, where the hostname is
        generated from the test case name. The HostRule will be linked to a
        PathMatcher, where stores the routing logic.

        Args:
            host_rule: A HostRule GCP resource as a JSON dict.
            path_matcher: A PathMatcher GCP resource as a JSON dict.

        Returns:
            A tuple contains the updated version of given HostRule and
            PathMatcher.
        """

    @abc.abstractmethod
    def xds_config_validate(
        self, xds_config: grpc_csds.DumpedXdsConfig
    ) -> None:
        """Validates received xDS config, if anything is wrong, raise.

        This stage only ends when the control plane failed to send a valid
        config within a given time range, like 600s.

        Args:
            xds_config: A DumpedXdsConfig instance can be used as a JSON dict,
              but also provides helper fields for commonly checked xDS config.
        """

    @abc.abstractmethod
    def rpc_distribution_validate(self, test_client: XdsTestClient) -> None:
        """Validates the routing behavior, if any is wrong, raise.

        Args:
            test_client: A XdsTestClient instance for all sorts of end2end testing.
        """

    @classmethod
    def hostname(cls):
        return "%s.%s:%s" % (
            cls.short_module_name,
            _split_camel(cls.__name__),
            GcpResourceManager().server_xds_port,
        )

    @classmethod
    def path_matcher_name(cls):
        # Path matcher name must match r'(?:[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?)'
        return "%s-%s-pm" % (cls.short_module_name, _split_camel(cls.__name__))

    @classmethod
    def setUpClass(cls):
        logging.info("----- Testing %s -----", cls.__name__)
        logging.info("Logs timezone: %s", time.localtime().tm_zone)

        lang_spec = xds_k8s_testcase.parse_lang_spec_from_flags()

        # Raises unittest.SkipTest if given client/server/version does not
        # support current test case.
        xds_k8s_testcase.evaluate_is_supported(lang_spec, cls.is_supported)

        # Configure cleanup to run after all tests regardless of
        # whether setUpClass failed.
        cls.addClassCleanup(cls.cleanupAfterTests)

        # Normally we don't want to make external calls in setUpClass.
        try:
            if not cls.started_test_cases:
                # Create the GCP resource once before the first test start
                GcpResourceManager().setup(cls.test_case_classes)
            cls.started_test_cases.add(cls.__name__)

            # Create the test case's own client runner with it's own namespace,
            # enables concurrent running with other test cases.
            cls.test_client_runner = (
                GcpResourceManager().create_test_client_runner()
            )
            # Start the client, and allow the test to override the initial
            # RPC config.
            rpc, metadata = cls.client_init_config(
                rpc="UnaryCall,EmptyCall", metadata=""
            )
            cls.test_client = cls.test_client_runner.run(
                server_target=f"xds:///{cls.hostname()}",
                rpc=rpc,
                metadata=metadata,
                qps=QPS.value,
                print_response=True,
            )
        except Exception as error:  # noqa pylint: disable=broad-except
            cls._log_class_hook_failure(error)
            raise

    @classmethod
    def cleanupAfterTests(cls):
        logging.info("----- TestCase %s teardown -----", cls.__name__)
        client_restarts: int = 0
        if cls.test_client_runner:
            try:
                logging.debug("Getting pods restart times")
                client_restarts = cls.test_client_runner.get_pod_restarts(
                    cls.test_client_runner.deployment
                )
            except (retryers.RetryError, k8s.NotFound) as e:
                logging.exception(e)

        cls.finished_test_cases.add(cls.__name__)
        # Whether to clean up shared pre-provisioned infrastructure too.
        # We only do it after all tests are finished.
        cleanup_all = cls.finished_test_cases == cls.test_case_names

        # Graceful cleanup: try three times, and don't fail the test on
        # a cleanup failure.
        retryer = retryers.constant_retryer(
            wait_fixed=_timedelta(seconds=10),
            attempts=3,
            log_level=logging.INFO,
        )
        try:
            retryer(cls._cleanup, cleanup_all)
        except retryers.RetryError:
            logging.exception("Got error during teardown")
        finally:
            if hasattr(cls, "test_client_runner") and cls.test_client_runner:
                logging.info("----- Test client logs -----")
                cls.test_client_runner.logs_explorer_run_history_links()

            # Fail if any of the pods restarted.
            error_msg = (
                "Client container unexpectedly restarted"
                f" {client_restarts} times during test."
                " In most cases, this is caused by the test client app crash."
            )
            assert client_restarts == 0, error_msg

    @classmethod
    def _cleanup(cls, cleanup_all: bool = False):
        if cls.test_client_runner:
            cls.test_client_runner.cleanup(force=True, force_namespace=True)
        if cleanup_all:
            GcpResourceManager().cleanup()

    def _fetch_and_check_xds_config(self):
        # TODO(lidiz) find another way to store last seen xDS config
        # Cleanup state for this attempt
        # pylint: disable=attribute-defined-outside-init
        self._client_config_dict = None
        # Fetch client config
        parsed = self.test_client.csds.fetch_client_status_parsed(
            log_level=logging.INFO
        )
        self.assertIsNotNone(parsed)
        # Found client config, test it.
        self._client_config_dict = parsed.client_config_dict
        # pylint: enable=attribute-defined-outside-init
        # Execute the child class provided validation logic
        self.xds_config_validate(parsed)

    def run(self, result: unittest.TestResult = None) -> None:
        """Abort this test case if CSDS check is failed.

        This prevents the test runner to waste time on RPC distribution test,
        and yields clearer signal.
        """
        if result.failures or result.errors:
            logging.info("Aborting %s", self.__class__.__name__)
        else:
            super().run(result)

    def test_client_config(self):
        self.test_client.wait_for_active_xds_channel(
            xds_server_uri=GcpResourceManager().xds_server_uri,
        )
        retryer = retryers.constant_retryer(
            wait_fixed=datetime.timedelta(
                seconds=_URL_MAP_PROPAGATE_CHECK_INTERVAL_SEC
            ),
            timeout=datetime.timedelta(seconds=_URL_MAP_PROPAGATE_TIMEOUT_SEC),
            logger=logging,
            log_level=logging.INFO,
        )
        try:
            retryer(self._fetch_and_check_xds_config)
        finally:
            logging.info(
                "latest xDS config:\n%s",
                GcpResourceManager().td.compute.resource_pretty_format(
                    self._client_config_dict
                ),
            )

    def test_rpc_distribution(self):
        self.rpc_distribution_validate(self.test_client)

    @classmethod
    def configure_and_send(
        cls,
        test_client: XdsTestClient,
        *,
        rpc_types: Sequence[str],
        metadata: Optional[grpc_testing.ConfigureMetadata] = None,
        app_timeout: Optional[int] = None,
        num_rpcs: int,
    ) -> grpc_testing.RpcDistributionStats:
        test_client.update_config.configure(
            rpc_types=rpc_types, metadata=metadata, app_timeout=app_timeout
        )
        # Configure RPC might race with get stats RPC on slower machines.
        time.sleep(_CLIENT_CONFIGURE_WAIT_SEC)
        lb_stats = test_client.get_load_balancer_stats(num_rpcs=num_rpcs)
        logging.info(
            "[%s] << Received LoadBalancerStatsResponse:\n%s",
            test_client.hostname,
            helpers_grpc.lb_stats_pretty(lb_stats),
        )
        return grpc_testing.RpcDistributionStats.from_message(lb_stats)

    def assertNumEndpoints(
        self,
        xds_config: grpc_csds.DumpedXdsConfig,
        k: int,
    ) -> None:
        self.assertLen(
            xds_config.endpoints,
            k,
            (
                "insufficient endpoints in EDS:"
                f" want={k} seen={xds_config.endpoints}"
            ),
        )

    def assertRpcStatusCode(  # pylint: disable=too-many-locals
        self,
        test_client: XdsTestClient,
        *,
        expected: Iterable[ExpectedResult],
        length: int,
        tolerance: float,
    ) -> None:
        """Assert the distribution of RPC statuses over a period of time."""
        # Sending with pre-set QPS for a period of time
        before_stats = test_client.get_load_balancer_accumulated_stats()
        logging.info(
            (
                "Received LoadBalancerAccumulatedStatsResponse from test client"
                " %s: before:\n%s"
            ),
            test_client.hostname,
            helpers_grpc.accumulated_stats_pretty(before_stats),
        )
        time.sleep(length)
        after_stats = test_client.get_load_balancer_accumulated_stats()
        logging.info(
            (
                "Received LoadBalancerAccumulatedStatsResponse from test client"
                " %s: after: \n%s"
            ),
            test_client.hostname,
            helpers_grpc.accumulated_stats_pretty(after_stats),
        )

        # Validate the diff
        for expected_result in expected:
            rpc = expected_result.rpc_type
            status = expected_result.status_code.value[0]
            # Compute observation
            # ProtoBuf messages has special magic dictionary that we don't need
            # to catch exceptions:
            # https://developers.google.com/protocol-buffers/docs/reference/python-generated#undefined
            seen_after = after_stats.stats_per_method[rpc].result[status]
            seen_before = before_stats.stats_per_method[rpc].result[status]
            seen = seen_after - seen_before
            # Compute total number of RPC started
            stats_per_method_after = after_stats.stats_per_method.get(
                rpc, {}
            ).result.items()
            total_after = sum(
                x[1] for x in stats_per_method_after
            )  # (status_code, count)
            stats_per_method_before = before_stats.stats_per_method.get(
                rpc, {}
            ).result.items()
            total_before = sum(
                x[1] for x in stats_per_method_before
            )  # (status_code, count)
            total = total_after - total_before
            # Compute and validate the number
            want = total * expected_result.ratio
            diff_ratio = abs(seen - want) / total
            self.assertLessEqual(
                diff_ratio,
                tolerance,
                (
                    f"Expect rpc [{rpc}] to return "
                    f"[{expected_result.status_code}] at "
                    f"{expected_result.ratio:.2f} ratio: "
                    f"seen={seen} want={want} total={total} "
                    f"diff_ratio={diff_ratio:.4f} > {tolerance:.2f}"
                ),
            )
