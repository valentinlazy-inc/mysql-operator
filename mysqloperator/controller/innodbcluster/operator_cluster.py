# Copyright (c) 2020, 2022, Oracle and/or its affiliates.
#
# Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl/
#


from typing import Any, Optional
from kopf._cogs.structs.bodies import Body
from kubernetes.client.rest import ApiException

from mysqloperator.controller.api_utils import ApiSpecError
from .. import consts, kubeutils, config, utils, errors, diagnose
from .. import shellutils
from ..group_monitor import g_group_monitor
from ..utils import g_ephemeral_pod_state
from ..kubeutils import api_core, api_apps, api_policy, api_rbac, api_cron_job, k8s_version
from ..backup import backup_objects
from ..config import DEFAULT_OPERATOR_VERSION_TAG
from .cluster_controller import ClusterController, ClusterMutex
from . import cluster_objects, router_objects, cluster_api
from .cluster_api import InnoDBCluster, InnoDBClusterSpec, MySQLPod, get_all_clusters
import kopf
from logging import Logger
import time


# TODO check whether we should store versions in status to make upgrade easier


def on_group_view_change(cluster: InnoDBCluster, members: list, view_id_changed: bool) -> None:
    """
    Triggered from the GroupMonitor whenever the membership view changes.
    This handler should react to changes that wouldn't be noticed by regular
    pod and cluster events.
    It also updates cluster status in the pods and cluster objects.
    """

    c = ClusterController(cluster)
    c.on_group_view_change(members, view_id_changed)


def monitor_existing_clusters(logger: Logger) -> None:
    clusters = cluster_api.get_all_clusters()
    for cluster in clusters:
        if cluster.get_create_time():
            g_group_monitor.monitor_cluster(
                cluster, on_group_view_change, logger)


@kopf.on.create(consts.GROUP, consts.VERSION,
                consts.INNODBCLUSTER_PLURAL)  # type: ignore
def on_innodbcluster_create(name: str, namespace: Optional[str], body: Body,
                            logger: Logger, **kwargs) -> None:
    logger.info(
        f"Initializing InnoDB Cluster name={name} namespace={namespace} on K8s {k8s_version()}")

    cluster = InnoDBCluster(body)

    # TODO: If we set the status here it will be emptied for unknown reasons later
    #       and hide other later set status (i.e. when using an invalid spec.version)
    #
    #cluster.set_status({
    #    "cluster": {
    #        "status":  diagnose.ClusterDiagStatus.INITIALIZING.value,
    #        "onlineInstances": 0,
    #        "lastProbeTime": utils.isotime()
    #    }})

    try:
        cluster.parse_spec()
        cluster.parsed_spec.validate(logger)
    except ApiSpecError as e:
        cluster.set_status({
            "cluster": {
                "status":  diagnose.ClusterDiagStatus.INVALID.value,
                "onlineInstances": 0,
                "lastProbeTime": utils.isotime()
            }})
        cluster.error(action="CreateCluster",
                      reason="InvalidArgument", message=str(e))
        raise kopf.TemporaryError(f"Error in InnoDBCluster spec: {e}")

    icspec = cluster.parsed_spec

    def ignore_404(f) -> Any:
        try:
            return f()
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    #print(f"Default operator IC edition: {config.MYSQL_OPERATOR_DEFAULT_IC_EDITION} Edition")
    cluster.log_cluster_info(logger)

    if not cluster.ready:
        try:
            print("0.Configuration ConfigMaps")
            for cm in cluster_objects.prepare_component_config_configmaps(cluster, logger):
                if not cluster.get_configmap(cm['metadata']['name']):
                    print(f"\tCreating...{cm}")
                    kopf.adopt(cm)
                    api_core.create_namespaced_config_map(namespace, cm)

            for secret in cluster_objects.prepare_component_config_secrets(cluster, logger):
                if not cluster.get_secret(secret['metadata']['name']):
                    print(f"\tCreating...{secret}")
                    kopf.adopt(secret)
                    api_core.create_namespaced_secret(namespace, secret)

            print("1. Initial Configuration ConfigMap and Container Probes")
            if not ignore_404(cluster.get_initconf):
                print("\tPreparing...")
                configs = cluster_objects.prepare_initconf(cluster, icspec, logger)
                print("\tCreating...")
                kopf.adopt(configs)
                api_core.create_namespaced_config_map(namespace, configs)

            print("2. Cluster Accounts")
            if not ignore_404(cluster.get_private_secrets):
                print("\tPreparing...")
                secret = cluster_objects.prepare_secrets(icspec)
                print("\tCreating...")
                kopf.adopt(secret)
                api_core.create_namespaced_secret(namespace=namespace, body=secret)

            print("3. Router Accounts")
            if not ignore_404(cluster.get_router_account):
                print("\tPreparing...")
                secret = router_objects.prepare_router_secrets(icspec)
                print("\tCreating...")
                kopf.adopt(secret)
                api_core.create_namespaced_secret(namespace=namespace, body=secret)

            print("4. Cluster Service")
            if not ignore_404(cluster.get_service):
                print("\tPreparing...")
                service = cluster_objects.prepare_cluster_service(icspec)
                print("\tCreating...")
                kopf.adopt(service)
                api_core.create_namespaced_service(namespace=namespace, body=service)

            print("5. Cluster ServiceAccount")
            if not ignore_404(cluster.get_service_account):
                print("\tPreparing...")
                sa = cluster_objects.prepare_service_account(icspec)
                if sa is None:
                    print(f"\tService account is predefined: {icspec.serviceAccountName}. Not creating")
                else:
                    print(f"\tCreating...{sa}")
                    kopf.adopt(sa)
                    api_core.create_namespaced_service_account(namespace=namespace, body=sa)

            print("6. Cluster RoleBinding")
            if not ignore_404(cluster.get_role_binding):
                print("\tPreparing...")
                rb = cluster_objects.prepare_role_binding(icspec)
                print(f"\tCreating...{rb}")
                kopf.adopt(rb)
                api_rbac.create_namespaced_role_binding(namespace=namespace, body=rb)

            print("7. Cluster StatefulSet")
            if not ignore_404(cluster.get_stateful_set):
                print("\tPreparing...")
                statefulset = cluster_objects.prepare_cluster_stateful_set(icspec, logger)
                print(f"\tCreating...{statefulset}")
                kopf.adopt(statefulset)

                api_apps.create_namespaced_stateful_set(namespace=namespace, body=statefulset)

            print("8. Cluster PodDisruptionBudget")
            if not ignore_404(cluster.get_disruption_budget):
                print("\tPreparing...")
                disruption_budget = cluster_objects.prepare_cluster_pod_disruption_budget(icspec)
                print("\tCreating...")
                kopf.adopt(disruption_budget)
                api_policy.create_namespaced_pod_disruption_budget(namespace=namespace, body=disruption_budget)

            print("9. Router Service")
            if not ignore_404(cluster.get_router_service):
                print("\tPreparing...")
                router_service = router_objects.prepare_router_service(icspec)
                print("\tCreating...")
                kopf.adopt(router_service)
                api_core.create_namespaced_service(namespace=namespace, body=router_service)

            print("10. Router Deployment")
            if not ignore_404(cluster.get_router_deployment):
                if icspec.router.instances > 0:
                    print("\tPreparing...")
                    # This will create the deployment but 0 instances. When the cluster is created (first
                    # instance joins it) the instance count will be set to icspec.router.instances
                    router_deployment = router_objects.prepare_router_deployment(cluster, logger, init_only=True)
                    print(f"\tCreating...{router_deployment}")
                    kopf.adopt(router_deployment)
                    api_apps.create_namespaced_deployment(namespace=namespace, body=router_deployment)
                else:
                    # If the user decides to set !0 routers, the routine that handles that that
                    # will create the deployment
                    print("\tRouter count is 0. No Deployment is created.")

            print("11. Backup Secrets")
            if not ignore_404(cluster.get_backup_account):
                print("\tPreparing...")
                secret = backup_objects.prepare_backup_secrets(icspec)
                print("\tCreating...")
                kopf.adopt(secret)
                api_core.create_namespaced_secret(namespace=namespace, body=secret)

        except Exception as exc:
            cluster.warn(action="CreateCluster", reason="CreateResourceFailed", message=f"{exc}")
            raise

        print(f"10. Setting operator version for the IC to {DEFAULT_OPERATOR_VERSION_TAG}")
        cluster.set_operator_version(DEFAULT_OPERATOR_VERSION_TAG)
        cluster.info(action="CreateCluster", reason="ResourcesCreated",
                     message="Dependency resources created, switching status to PENDING")
        cluster.set_status({
            "cluster": {
                "status":  diagnose.ClusterDiagStatus.PENDING.value,
                "onlineInstances": 0,
                "lastProbeTime": utils.isotime()
            }})


@kopf.on.delete(consts.GROUP, consts.VERSION,
                consts.INNODBCLUSTER_PLURAL)  # type: ignore
def on_innodbcluster_delete(name: str, namespace: str, body: Body,
                            logger: Logger, **kwargs):
    cluster = InnoDBCluster(body)

    logger.info(f"Deleting cluster {name}")

    g_group_monitor.remove_cluster(cluster)

    # Scale down routers to 0
    logger.info(f"Updating Router Deployment.replicas to 0")
    router_objects.update_size(cluster, 0, logger)

    # Scale down the cluster to 0
    sts = cluster.get_stateful_set()
    if sts:
        logger.info(f"Updating InnoDB Cluster StatefulSet.instances to 0")
        cluster_objects.update_stateful_set_spec(
            sts, {"spec": {"replicas": 0}})


# TODO add a busy state and prevent changes while on it

@kopf.on.field(consts.GROUP, consts.VERSION, consts.INNODBCLUSTER_PLURAL,
               field="spec.instances")  # type: ignore
def on_innodbcluster_field_instances(old, new, body: Body,
                                     logger: Logger, **kwargs):
    cluster = InnoDBCluster(body)

    # ignore spec changes if the cluster is still being initialized
    if not cluster.ready:
        logger.debug(f"Ignoring spec.instances change for unready cluster")
        return

    # TODO - identify what cluster statuses should allow changes to the size of the cluster

    sts = cluster.get_stateful_set()
    if sts and old != new:
        logger.info(
            f"Updating InnoDB Cluster StatefulSet.replicas from {old} to {new}")
        cluster.parsed_spec.validate(logger)

        cluster_objects.update_stateful_set_spec(
            sts, {"spec": {"replicas": new}})


@kopf.on.field(consts.GROUP, consts.VERSION, consts.INNODBCLUSTER_PLURAL,
               field="spec.version")  # type: ignore
def on_innodbcluster_field_version(old, new, body: Body,
                                   logger: Logger, **kwargs):
    cluster = InnoDBCluster(body)

    # ignore spec changes if the cluster is still being initialized
    if not cluster.ready:
        logger.debug(f"Ignoring spec.version change for unready cluster")
        return

    # TODO - identify what cluster statuses should allow this change

    sts = cluster.get_stateful_set()
    if sts and old != new:
        logger.info(
            f"Propagating spec.version={new} for {cluster.namespace}/{cluster.name} (was {old})")

        cluster.parse_spec()
        cluster_ctl = ClusterController(cluster)
        try:
            cluster_ctl.on_server_version_change(new)
        except:
            # revert version in the spec
            raise
        cluster_objects.update_mysql_image(sts, cluster.parsed_spec, logger)
        router_deploy = cluster.get_router_deployment()
        if router_deploy:
            router_objects.update_router_image(router_deploy, cluster.parsed_spec, logger)


@kopf.on.field(consts.GROUP, consts.VERSION, consts.INNODBCLUSTER_PLURAL,
               field="spec.imageRepository")  # type: ignore
def on_innodbcluster_field_image_repository(old, new, body: Body,
                                            logger: Logger, **kwargs):
    cluster = InnoDBCluster(body)

    # ignore spec changes if the cluster is still being initialized
    if not cluster.ready:
        logger.debug(f"Ignoring spec.imageRepository change for unready cluster")
        return

    sts = cluster.get_stateful_set()
    if sts and old != new:
        logger.info(
            f"Propagating spec.imageRepository={new} for {cluster.namespace}/{cluster.name} (was {old})")

        cluster.parse_spec()

        cluster_objects.update_mysql_image(sts, cluster.parsed_spec, logger)
        cluster_objects.update_operator_image(sts, cluster.parsed_spec)
        router_deploy = cluster.get_router_deployment()
        if router_deploy:
            router_objects.update_router_image(router_deploy, cluster.parsed_spec, logger)


@kopf.on.field(consts.GROUP, consts.VERSION, consts.INNODBCLUSTER_PLURAL,
               field="spec.imagePullPolicy")  # type: ignore
def on_innodbcluster_field_image_pull_policy(old, new, body: Body,
                                            logger: Logger, **kwargs):
    cluster = InnoDBCluster(body)

    # ignore spec changes if the cluster is still being initialized
    if not cluster.ready:
        logger.debug(f"Ignoring spec.imagePullPolicy change for unready cluster")
        return

    sts = cluster.get_stateful_set()
    if sts and old != new:
        logger.info(
            f"Propagating spec.imagePullPolicy={new} for {cluster.namespace}/{cluster.name} (was {old})")

        cluster.parse_spec()

        cluster_objects.update_pull_policy(sts, cluster.parsed_spec, logger)
        router_deploy = cluster.get_router_deployment()
        if router_deploy:
            router_objects.update_pull_policy(router_deploy, cluster.parsed_spec, logger)


@kopf.on.field(consts.GROUP, consts.VERSION, consts.INNODBCLUSTER_PLURAL,
               field="spec.image")  # type: ignore
def on_innodbcluster_field_image(old, new, body: Body,
                                 logger: Logger, **kwargs):
    cluster = InnoDBCluster(body)

    # ignore spec changes if the cluster is still being initialized
    if not cluster.ready:
        logger.debug(f"Ignoring spec.image change for unready cluster")
        return

    # TODO - identify what cluster statuses should allow this change

    sts = cluster.get_stateful_set()
    if sts and old != new:
        logger.info(
            f"Updating MySQL image for InnoDB Cluster StatefulSet pod template from {old} to {new}")
        cluster.parsed_spec.validate(logger)

        cluster_ctl = ClusterController(cluster)

        try:
            cluster_ctl.on_server_image_change(new)
        except:
            # revert version in the spec
            raise

        cluster_objects.update_mysql_image(sts, cluster.parsed_spec, logger)


@kopf.on.field(consts.GROUP, consts.VERSION, consts.INNODBCLUSTER_PLURAL,
               field="spec.router.instances")  # type: ignore
def on_innodbcluster_field_router_instances(old: int, new: int, body: Body,
                                            logger: Logger, **kwargs):
    cluster = InnoDBCluster(body)

    # ignore spec changes if the cluster is still being initialized
    if not cluster.get_create_time():
        logger.debug(
            f"Ignoring spec.router.instances change for unready cluster")
        return

    with ClusterMutex(cluster):
        logger.info(f"Updating Router Deployment.replicas from {old} to {new}")
        cluster.parsed_spec.validate(logger)

        router_objects.update_size(cluster, new, logger)


@kopf.on.field(consts.GROUP, consts.VERSION, consts.INNODBCLUSTER_PLURAL,
               field="spec.router.version")  # type: ignore
def on_innodbcluster_field_router_version(old: str, new: str, body: Body,
                                          logger: Logger, **kwargs):
    if old == new:
        return

    cluster = InnoDBCluster(body)

    # ignore spec changes if the cluster is still being initialized
    if not cluster.get_create_time():
        logger.debug(
            f"Ignoring spec.router.version change for unready cluster")
        return

    cluster.parsed_spec.validate(logger)
    with ClusterMutex(cluster):
        router_deploy = cluster.get_router_deployment()
        if router_deploy:
            router_objects.update_router_image(router_deploy, cluster.parsed_spec, logger)



@kopf.on.field(consts.GROUP, consts.VERSION, consts.INNODBCLUSTER_PLURAL,
               field="spec.backupSchedules")  # type: ignore
def on_innodbcluster_field_backup_schedules(old: str, new: str, body: Body,
                                          logger: Logger, **kwargs):
    if old == new:
        return

    logger.info("on_innodbcluster_field_backup_schedules")
    cluster = InnoDBCluster(body)

    # Ignore spec changes if the cluster is still being initialized
    # This handler will be called even when the cluster is being initialized as the
    # `old` value will be None and the `new` value will be the schedules that the cluster has.
    # This makes it possible to create them here and not in on_innodbcluster_create().
    # There in on_innodbcluster_create(), only the objects which are critical for the creation
    # of the server should be created.
    # After the cluster is ready we will add the schedules. This also allows to have the schedules
    # created (especially when `enabled`) after the cluster has been created, solving issues with
    # cron job not bein called or cron jobs being created as suspended and then when the cluster is
    # running to be enabled again - which would end to be a 2-step process.
    # The cluster is created after the first instance is up and running. Thus,
    # don't need to take actions in post_create_actions() in the cluster controller
    # but async await for Kopf to call again this handler.
    if not cluster.get_create_time():
        raise kopf.TemporaryError("The cluster is not ready. Will create the schedules once the first instance is up and running", delay=10)

    cluster.parsed_spec.validate(logger)
    with ClusterMutex(cluster):
        backup_objects.update_schedules(cluster.parsed_spec, old, new, logger)


def update_tls_field(body: Body, field: str, logger: Logger) -> None:
    cluster = InnoDBCluster(body)

    if not cluster.get_create_time():
        logger.debug(
            f"Ignoring {field} change for unready cluster")
        return

    cluster.parsed_spec.validate(logger)

    cluster_objects.reconcile_stateful_set(cluster, logger)


@kopf.on.field(consts.GROUP, consts.VERSION, consts.INNODBCLUSTER_PLURAL,
               field="spec.tlsUseSelfSigned")  # type: ignore
def on_innodbcluster_field_tls_use_self_signed(body: Body,
                                               logger: Logger, **kwargs):
    logger.info("on_innodbcluster_field_tls_use_self_signed")
    update_tls_field(body, "spec.tlsUseSelfSigned", logger)


@kopf.on.field(consts.GROUP, consts.VERSION, consts.INNODBCLUSTER_PLURAL,
               field="spec.tlsSecretName")  # type: ignore
def on_innodbcluster_field_tls_secret_name(body: Body,
                                          logger: Logger, **kwargs):
    logger.info("on_innodbcluster_field_tls_secret_name")
    update_tls_field(body, "spec.tlsSecretName", logger)


@kopf.on.field(consts.GROUP, consts.VERSION, consts.INNODBCLUSTER_PLURAL,
               field="spec.router.tlsSecretName")  # type: ignore
def on_innodbcluster_field_router_tls_secret_name(body: Body,
                                                  logger: Logger, **kwargs):
    logger.info("on_innodbcluster_field_router_tls_secret_name")
    update_tls_field(body, "spec.router.tlsSecretName", logger)


@kopf.on.field(consts.GROUP, consts.VERSION, consts.INNODBCLUSTER_PLURAL,
               field="spec.tlsCASecretName")  # type: ignore
def on_innodbcluster_field_tls_ca_secret_name(body: Body,
                                              logger: Logger, **kwargs):
    logger.info("on_innodbcluster_field_tls_ca_secret_name")
    update_tls_field(body, "spec.tlsCASecretName", logger)


@kopf.on.create("", "v1", "pods",
                labels={"component": "mysqld"})  # type: ignore
def on_pod_create(body: Body, logger: Logger, **kwargs):
    """
    Handle MySQL server Pod creation, which can happen when:
    - cluster is being first created
    - cluster is being scaled up (more members added)
    """

    # TODO ensure that the pod is owned by us
    pod = MySQLPod.from_json(body)

    # check general assumption
    assert not pod.deleting

    logger.info(f"POD CREATED: pod={pod.name} ContainersReady={pod.check_condition('ContainersReady')} Ready={pod.check_condition('Ready')} gate[configured]={pod.get_member_readiness_gate('configured')}")

    configured = pod.get_member_readiness_gate("configured")
    if not configured:
        # TODO add extra diagnostics about why the pod is not ready yet, for
        # example, unbound volume claims, initconf not finished etc
        raise kopf.TemporaryError(f"Sidecar of {pod.name} is not yet configured", delay=30)

    # If we are here all containers have started. This means, that if we are initializing
    # the database from a donor (cloning) the sidecar has already started a seed instance
    # and cloned from the donor into it (see initdb.py::start_clone_seed_pod())
    cluster = pod.get_cluster()
    logger.info(f"CLUSTER DELETING={cluster.deleting}")

    assert cluster

    with ClusterMutex(cluster, pod):
        first_pod = pod.index == 0 and not cluster.get_create_time()
        if first_pod:
            cluster_objects.on_first_cluster_pod_created(cluster, logger)

            g_group_monitor.monitor_cluster(
                cluster, on_group_view_change, logger)

        cluster_ctl = ClusterController(cluster)

        cluster_ctl.on_pod_created(pod, logger)

        # Remember how many restarts happened as of now
        g_ephemeral_pod_state.set(pod, "mysql-restarts", pod.get_container_restarts("mysql"), context="on_pod_create")


@kopf.on.event("", "v1", "pods",
               labels={"component": "mysqld"})  # type: ignore
def on_pod_event(event, body: Body, logger: Logger, **kwargs):
    """
    Handle low-level MySQL server pod events. The events we're interested in are:
    - when a container restarts in a Pod (e.g. because of mysqld crash)
    """
    # TODO ensure that the pod is owned by us

    while True:
        try:
            pod = MySQLPod.from_json(body)

            member_info = pod.get_membership_info()
            ready = pod.check_containers_ready()
            if pod.phase != "Running" or pod.deleting or not member_info:
                logger.debug(
                    f"ignored pod event: pod={pod.name} containers_ready={ready} deleting={pod.deleting} phase={pod.phase} member_info={member_info}")
                return

            mysql_restarts = pod.get_container_restarts("mysql")

            event = ""
            if g_ephemeral_pod_state.get(pod, "mysql-restarts") != mysql_restarts:
                event = "mysql-restarted"

            containers = [
                f"{c.name}={'ready' if c.ready else 'not-ready'}" for c in pod.status.container_statuses]
            conditions = [
                f"{c.type}={c.status}" for c in pod.status.conditions]
            logger.debug(f"POD EVENT {event}: pod={pod.name} containers_ready={ready} deleting={pod.deleting} phase={pod.phase} member_info={member_info} restarts={mysql_restarts} containers={containers} conditions={conditions}")

            cluster = pod.get_cluster()
            if not cluster:
                logger.info(
                    f"Ignoring event for pod {pod.name} belonging to a deleted cluster")
                return
            with ClusterMutex(cluster, pod):
                cluster_ctl = ClusterController(cluster)

                # Check if a container in the pod restarted
                if ready and event == "mysql-restarted":
                    cluster_ctl.on_pod_restarted(pod, logger)

                    g_ephemeral_pod_state.set(pod, "mysql-restarts", mysql_restarts, context="on_pod_event")

                # Check if we should refresh the cluster status
                status = cluster_ctl.probe_status_if_needed(pod, logger)
                if status == diagnose.ClusterDiagStatus.UNKNOWN:
                    raise kopf.TemporaryError(
                        f"Cluster has unreachable members. status={status}", delay=15)
                break
        except kopf.TemporaryError as e:
            # TODO review this
            # Manually handle retries, the event handler isn't getting called again
            # by kopf (maybe a bug or maybe we're using it wrong)
            logger.info(f"{e}: retrying after {e.delay} seconds")
            if e.delay:
                time.sleep(e.delay)
            continue


@kopf.on.delete("", "v1", "pods",
                labels={"component": "mysqld"})  # type: ignore
def on_pod_delete(body: Body, logger: Logger, **kwargs):
    """
    Handle MySQL server Pod deletion, which can happen when:
    - cluster is being scaled down (members being removed)
    - cluster is being deleted
    - user deletes a pod by hand
    """
    # TODO ensure that the pod is owned by us
    pod = MySQLPod.from_json(body)

    # check general assumption
    assert pod.deleting

    # removeInstance the pod
    cluster = pod.get_cluster()

    if cluster:
        with ClusterMutex(cluster, pod):
            cluster_ctl = ClusterController(cluster)

            cluster_ctl.on_pod_deleted(pod, body, logger)

            if pod.index == 0 and cluster.deleting:
                cluster_objects.on_last_cluster_pod_removed(cluster, logger)
    else:
        pod.remove_member_finalizer(body)

        logger.error(f"Owner cluster for {pod.name} does not exist anymore")

# An example of a `when` hook for finding secrets belonging to a IC
#
#def secret_belongs_to_a_cluster_checker(meta, namespace:str, name, logger: Logger, **_) -> bool:
#    clusters = get_all_clusters(namespace)
#    for cluster in clusters:
#        if name in (cluster.parsed_spec.tlsCASecretName,
#                    cluster.parsed_spec.tlsSecretName,
#                    cluster.parsed_spec.router.tlsSecretName):
#            return True
#    return False
#
# Use like the following
#@kopf.on.create("", "v1", "secrets", when=secret_belongs_to_a_cluster_checker) # type: ignore
#@kopf.on.update("", "v1", "secrets", when=secret_belongs_to_a_cluster_checker) # type: ignore
#
