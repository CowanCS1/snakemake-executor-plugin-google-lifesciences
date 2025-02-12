__author__ = "Vanessa Sochat, Johannes Köster"
__copyright__ = "Copyright 2023, Snakemake community"
__email__ = "johannes.koester@uni-due.de"
__license__ = "MIT"

from dataclasses import dataclass, field
import hashlib
import math
import os
import re
import shutil
import tarfile
import tempfile
import time
from typing import List, Generator, Optional
import uuid

from googleapiclient.discovery import build as discovery_build
from google.cloud import storage
import google.auth
import google_auth_httplib2
import httplib2
import googleapiclient
import google
from google.api_core import retry

from snakemake_executor_plugin_google_lifesciences.common import bytesto, google_cloud_retry_predicate

from snakemake_interface_executor_plugins.executors.base import SubmittedJobInfo
from snakemake_interface_executor_plugins.executors.remote import RemoteExecutor
from snakemake_interface_executor_plugins import ExecutorSettingsBase, CommonSettings
from snakemake_interface_executor_plugins.workflow import WorkflowExecutorInterface
from snakemake_interface_executor_plugins.logging import LoggerExecutorInterface
from snakemake_interface_executor_plugins.jobs import (
    ExecutorJobInterface,
)
from snakemake_interface_common.exceptions import WorkflowError


# Optional:
# define additional settings for your executor
# They will occur in the Snakemake CLI as --<executor-name>-<param-name>
# Omit this class if you don't need any.
@dataclass
class ExecutorSettings(ExecutorSettingsBase):
    regions: List[str] = field(
        default=["us-east1", "us-west1", "us-central1"],
        metadata={
            "help": "One or more valid instance regions (defaults to US).",
            "required": True,
        }
    )
    location: str = field(
        default=None,
        metadata={
            "help": "The Life Sciences API service used to schedule the jobs. "
        " E.g., us-centra1 (Iowa) and europe-west2 (London) "
        " Watch the terminal output to see all options found to be available. "
        " If not specified, defaults to the first found with a matching prefix "
        " from regions specified with --google-lifesciences-regions.",
        }
    )
    keep_source_cache: bool = field(
        default=False,
        metadata={
            "help": "Cache workflows in your Google Cloud Storage Bucket specified "
            "by --default-remote-prefix/{source}/{cache}. Each workflow working "
            "directory is compressed to a .tar.gz, named by the hash of the "
            "contents, and kept in Google Cloud Storage. By default, the caches "
            "are deleted at the shutdown step of the workflow."
        }
    )
    service_account_email: Optional[str] = field(
        default=None,
        metadata={
            "help": "A service account email address."
        }
    )
    network: Optional[str] = field(
        default=None,
        metadata={
            "help": "Network to use in Google Compute Engine VM instance."
        }
    )
    subnetwork: Optional[str] = field(
        default=None,
        metadata={
            "help": "Subnetwork to use in Google Compute Engine VM instance."
        }
    )


# Required:
# Specify common settings shared by various executors.
common_settings = CommonSettings(
    # define whether your executor plugin executes locally
    # or remotely. In virtually all cases, it will be remote execution
    # (cluster, cloud, etc.). Only Snakemake's standard execution
    # plugins (snakemake-executor-plugin-dryrun, snakemake-executor-plugin-local)
    # are expected to specify False here.
    non_local_exec=True,
    # Define whether your executor plugin implies that there is no shared
    # filesystem (True) or not (False).
    # This is e.g. the case for cloud execution.
    implies_no_shared_fs=True,
)


# Required:
# Implementation of your executor
class Executor(RemoteExecutor):
    def __init__(
        self,
        workflow: WorkflowExecutorInterface,
        logger: LoggerExecutorInterface,
    ):
        super().__init__(
            workflow,
            logger,
            # configure behavior of RemoteExecutor below
            # whether arguments for setting the remote provider shall  be passed to jobs
            pass_default_remote_provider_args=True,
            # whether arguments for setting default resources shall be passed to jobs
            pass_default_resources_args=True,
            # whether environment variables shall be passed to jobs
            pass_envvar_declarations_to_cmd=False,
        )

        self.preemptible = self.workflow.remote_execution_settings.preemptible_rules

        # Prepare workflow sources for build package
        self._set_workflow_sources()

        # Attach variables for easy access
        self.quiet = workflow.output_settings.quiet
        self.workdir = os.path.realpath(os.path.dirname(self.workflow.persistence.path))
        self._save_storage_cache = self.workflow.executor_settings.keep_source_cache

        # IMPORTANT: using Compute Engine API and not k8s == no support for secrets
        self.envvars = list(self.workflow.envvars) or []

        # Quit early if we can't authenticate
        self._get_services()
        self._get_bucket()

        # Akin to Kubernetes, create a run namespace, default container image
        self.run_namespace = str(uuid.uuid4())
        self.container_image = self.workflow.remote_execution_settings.container_image
        logger.info(f"Using {self.container_image} for Google Life Science jobs.")
        self.regions = self.workflow.executor_settings.regions

        # The project name is required, either from client or environment
        self.project = (
            os.environ.get("GOOGLE_CLOUD_PROJECT") or self._bucket_service.project
        )
        # Determine API location based on user preference, and then regions
        self._set_location(self.workflow.executor_settings.location)
        # Tell the user right away the regions, location, and container
        logger.debug("regions=%s" % self.regions)
        logger.debug("location=%s" % self.location)
        logger.debug("container=%s" % self.container_image)

        # If specified, capture service account and GCE VM network configuration
        self.service_account_email = self.workflow.executor_settings.service_account_email
        self.network = self.workflow.executor_settings.network
        self.subnetwork = self.workflow.executor_settings.subnetwork

        # Log service account and VM network configuration
        logger.debug("service_account_email=%s" % self.service_account_email)
        logger.debug("network=%s" % self.network)
        logger.debug("subnetwork=%s" % self.subnetwork)

        # Keep track of build packages to clean up shutdown, and generate
        self._build_packages = set()
        targz = self._generate_build_source_package()
        self._upload_build_source_package(targz)


    def shutdown(self):
        """
        Shutdown deletes build packages if the user didn't request to clean
        up the cache. At this point we've already cancelled running jobs.
        """

        @retry.Retry(predicate=google_cloud_retry_predicate)
        def _shutdown():
            # Delete build source packages only if user regooglquested no cache
            if self._save_storage_cache:
                self.logger.debug("Requested to save workflow sources, skipping cleanup.")
            else:
                for package in self._build_packages:
                    blob = self.bucket.blob(package)
                    if blob.exists():
                        self.logger.debug("Deleting blob %s" % package)
                        blob.delete()

            # perform additional steps on shutdown if necessary

        _shutdown()

        super().shutdown()


    def run_job(self, job: ExecutorJobInterface):
        # Implement here how to run a job.
        # You can access the job's resources, etc.
        # via the job object.
        # After submitting the job, you have to call
        # self.report_job_submission(job_info).
        # with job_info being of type
        # snakemake_interface_executor_plugins.executors.base.SubmittedJobInfo.

        # https://cloud.google.com/life-sciences/docs/reference/rest/v2beta/projects.locations.pipelines
        pipelines = self._api.projects().locations().pipelines()

        # pipelines.run
        # https://cloud.google.com/life-sciences/docs/reference/rest/v2beta/projects.locations.pipelines/run

        labels = self._generate_pipeline_labels(job)
        pipeline = self._generate_pipeline(job)

        # The body of the request is a Pipeline and labels
        body = {"pipeline": pipeline, "labels": labels}

        # capabilities - this won't currently work (Singularity in Docker)
        # We either need to add CAPS or run in privileged mode (ehh)
        if job.needs_singularity and self.workflow.deployment_settings.use_singularity:
            raise WorkflowError(
                "Singularity requires additional capabilities that "
                "aren't yet supported for standard Docker runs, and "
                "is not supported for the Google Life Sciences executor."
            )

        # location looks like: "projects/<project>/locations/<location>"
        operation = pipelines.run(parent=self.location, body=body)

        # 403 will result if no permission to use pipelines or project
        result = self._retry_request(operation)

        # The jobid is the last number of the full name
        jobid = result["name"].split("/")[-1]

        # Give some logging for how to get status
        self.logger.info(
            "Get status with:\n"
            "gcloud config set project {project}\n"
            "gcloud beta lifesciences operations describe {location}/operations/{jobid}\n"
            "gcloud beta lifesciences operations list\n"
            "Logs will be saved to: {bucket}/{logdir}\n".format(
                project=self.project,
                jobid=jobid,
                location=self.location,
                bucket=self.bucket.name,
                logdir=self.gs_logs,
            )
        )

        job_info = SubmittedJobInfo(
            job=job,
            external_jobid=jobid,
            aux={"external_jobname": result["name"]},
        )
        self.report_job_submission(job_info)

    async def check_active_jobs(
        self, active_jobs: List[SubmittedJobInfo]
    ) -> Generator[SubmittedJobInfo, None, None]:
        # Check the status of active jobs.

        # You have to iterate over the given list active_jobs.
        # For jobs that have finished successfully, you have to call
        # self.report_job_success(job).
        # For jobs that have errored, you have to call
        # self.report_job_error(job).
        # Jobs that are still running have to be yielded.
        #
        # For queries to the remote middleware, please use
        # self.status_rate_limiter like this:
        #
        # async with self.status_rate_limiter:
        #    # query remote middleware here
        for j in active_jobs:
            async with self.status_rate_limiter:
                # https://cloud.google.com/life-sciences/docs/reference/rest/v2beta/projects.locations.operations/get
                # Get status from projects.locations.operations/get
                operations = self._api.projects().locations().operations()
                request = operations.get(name=j.jobname)
                self.logger.debug(f"Checking status for operation {j.jobid}")

                try:
                    status = self._retry_request(request)
                except googleapiclient.errors.HttpError as ex:
                    # Operation name not found, even finished should be found
                    if ex.status == 404:
                        j.error_callback(j.job)
                        continue

                    # Unpredictable server (500) error
                    elif ex.status == 500:
                        msg = ex["content"].decode("utf-8")
                        self.report_job_error(j, msg=msg)

                except WorkflowError as ex:
                    self.report_job_error(j, msg=str(ex))
                    continue

            if status.get("done", False) == True:
                # The operation is done
                # Derive success/failure from status codes (prints too)
                if self._job_was_successful(status):
                    self.report_job_success(j)
                else:
                    self.report_job_error(j)
            else:
                # still running
                yield j

    def cancel_jobs(self, active_jobs: List[SubmittedJobInfo]):
        # Cancel all active jobs.
        # This method is called when Snakemake is interrupted.
        
        # projects.locations.operations/cancel
        operations = self._api.projects().locations().operations()

        for job in active_jobs:
            request = operations.cancel(name=job.aux["external_jobname"])
            self.logger.debug(f"Cancelling operation {job.external_jobid}")
            try:
                self._retry_request(request)
            except (Exception, BaseException, googleapiclient.errors.HttpError):
                continue

    def _job_was_successful(self, status):
        """
        Based on a status response (a [pipeline].projects.locations.operations.get
        debug print the list of events, return True if all return codes 0
        and False otherwise (indication of failure). In that a nonzero exit
        status is found, we also debug print it for the user.
        """
        success = True

        # https://cloud.google.com/life-sciences/docs/reference/rest/v2beta/Event
        for event in status["metadata"]["events"]:
            self.logger.debug(event["description"])

            # Does it always result in fail for other failure reasons?
            if "failed" in event:
                success = False
                action = event.get("failed")
                self.logger.debug("{}: {}".format(action["code"], action["cause"]))

            elif "unexpectedExitStatus" in event:
                action = event.get("unexpectedExitStatus")

                if action["exitStatus"] != 0:
                    success = False

                    # Provide reason for the failure (desc includes exit code)
                    msg = "%s" % event["description"]
                    if "stderr" in action:
                        msg += ": %s" % action["stderr"]
                        self.logger.debug(msg)

        return success

    def _retry_request(self, request, timeout=2, attempts=3):
        """
        The Google Python API client frequently has BrokenPipe errors. This
        function takes a request, and executes it up to number of retry,
        each time with a 2* increase in timeout.

        Parameters
        ==========
        request: the Google Cloud request that needs to be executed
        timeout: time to sleep (in seconds) before trying again
        attempts: remaining attempts, throw error when hit 0
        """

        try:
            return request.execute()
        except Exception as ex:
            if attempts > 0:
                time.sleep(timeout)
                return self._retry_request(
                    request, timeout=timeout * 2, attempts=attempts - 1
                )
            raise WorkflowError(ex)

    def get_available_machine_types(self):
        """
        Using the regions available at self.regions, use the GCP API
        to retrieve a lookup dictionary of all available machine types.
        """
        # Regular expression to determine if zone in region
        regexp = "^(%s)" % "|".join(self.regions)

        # Retrieve zones, filter down to selected regions
        zones = self._retry_request(
            self._compute_cli.zones().list(project=self.project)
        )
        zones = [z for z in zones["items"] if re.search(regexp, z["name"])]

        # Retrieve machine types available across zones
        # https://cloud.google.com/compute/docs/regions-zones/
        lookup = {}
        for zone in zones:
            request = self._compute_cli.machineTypes().list(
                project=self.project, zone=zone["name"]
            )
            lookup[zone["name"]] = self._retry_request(request)["items"]

        # Only keep those that are shared, use last zone as a base
        machine_types = {mt["name"]: mt for mt in lookup[zone["name"]]}
        del lookup[zone["name"]]

        # Update final list based on the remaining
        to_remove = set()
        for zone, types in lookup.items():
            names = [x["name"] for x in types]
            names = [name for name in names if "micro" not in name]
            names = [name for name in names if not re.search("^(e2|m1)", name)]
            for machine_type in list(machine_types.keys()):
                if machine_type not in names:
                    to_remove.add(machine_type)

        for machine_type in to_remove:
            del machine_types[machine_type]
        return machine_types
    
    def _add_gpu(self, gpu_count):
        """
        Add a number of NVIDIA gpus to the current executor. This works
        by way of adding nvidia_gpu to the job default resources, and also
        changing the default machine type prefix to be n1, which is
        the currently only supported instance type for using GPUs for LHS.
        """
        if not gpu_count or gpu_count == 0:
            return

        self.logger.debug(
            "found resource request for {} GPUs. This will limit to n1 "
            "instance types.".format(gpu_count)
        )
        self.workflow.resource_settings.default_resources.set_resource(
            "nvidia_gpu", gpu_count
        )

        self._machine_type_prefix = self._machine_type_prefix or ""
        if not self._machine_type_prefix.startswith("n1"):
            self._machine_type_prefix = "n1"

    def _generate_job_resources(self, job: ExecutorJobInterface):
        """
        Given a particular job, generate the resources that it needs,
        including default regions and the virtual machine configuration
        """
        # Right now, do a best effort mapping of resources to instance types
        cores = job.resources.get("_cores", 1)
        if "mem_mb" not in job.resources:
            raise WorkflowError(
                f"No memory resource (mem, mem_mb) defined for job from "
                "rule {job.rule.name}. Make sure to use --default-resources."
            )
        mem_mb = job.resources["mem_mb"]

        if "disk_mb" not in job.resources:
            raise WorkflowError(
                f"No disk resource (disk, disk_mb) defined for job from rule "
                "{job.rule.name}. Make sure to use --default-resources."
            )
        # IOPS performance proportional to disk size
        disk_mb = job.resources["disk_mb"]

        # Convert mb to gb
        disk_gb = math.ceil(disk_mb / 1024)

        # Look for if the user wants an nvidia gpu
        gpu_count = job.resources.get("nvidia_gpu") or job.resources.get("gpu")
        gpu_model = job.resources.get("gpu_model")

        # If a gpu model is specified without a count, we assume 1
        if gpu_model and not gpu_count:
            gpu_count = 1

        # Job resource specification can be overridden by gpu preferences
        self.machine_type_prefix = job.resources.get("machine_type")

        # If gpu wanted, limit to N1 general family, and update arguments
        if gpu_count:
            self._add_gpu(gpu_count)

        machine_types = self.get_available_machine_types()

        # Alert the user of machine_types available before filtering
        # https://cloud.google.com/compute/docs/machine-types
        self.logger.debug(
            "found {} machine types across regions {} before filtering "
            "to increase selection, define fewer regions".format(
                len(machine_types), self.regions
            )
        )

        # First pass - eliminate anything that too low in cpu/memory
        keepers = dict()

        # Also keep track of max cpus and memory, in case none available
        max_cpu = 1
        max_mem = 15360

        for name, machine_type in machine_types.items():
            max_cpu = max(max_cpu, machine_type["guestCpus"])
            max_mem = max(max_mem, machine_type["memoryMb"])
            if machine_type["guestCpus"] < cores or machine_type["memoryMb"] < mem_mb:
                continue
            keepers[name] = machine_type

        # If a prefix is set, filter down to it
        if self.machine_type_prefix:
            machine_types = keepers
            keepers = dict()
            for name, machine_type in machine_types.items():
                if name.startswith(self.machine_type_prefix):
                    keepers[name] = machine_type

        # If we don't have any contenders, workflow error
        if not keepers:
            if self.machine_type_prefix:
                raise WorkflowError(
                    "Machine prefix {prefix} is too strict, or the resources cannot "
                    " be satisfied, so there are no options "
                    "available.".format(prefix=self.machine_type_prefix)
                )
            else:
                raise WorkflowError(
                    "You requested {requestMemory} MB memory, {requestCpu} cores. "
                    "The maximum available are {availableMemory} MB memory and "
                    "{availableCpu} cores. These resources cannot be satisfied. "
                    "Please consider reducing the resource requirements of the "
                    "corresponding rule.".format(
                        requestMemory=mem_mb,
                        requestCpu=cores,
                        availableCpu=max_cpu,
                        availableMemory=max_mem,
                    )
                )

        # Now find (quasi) minimal to satisfy constraints
        machine_types = keepers

        # Select the first as the "smallest"
        smallest = list(machine_types.keys())[0]
        min_cores = machine_types[smallest]["guestCpus"]
        min_mem = machine_types[smallest]["memoryMb"]

        for name, machine_type in machine_types.items():
            if (
                machine_type["guestCpus"] < min_cores
                and machine_type["memoryMb"] < min_mem
            ):
                smallest = name
                min_cores = machine_type["guestCpus"]
                min_mem = machine_type["memoryMb"]

        selected = machine_types[smallest]
        self.logger.debug(
            "Selected machine type {}:{}".format(smallest, selected["description"])
        )

        if job.is_group():
            preemptible = all(self.preemptible_rules.is_preemptible(rule.name) for rule in job.rules)
            if not preemptible and any(
                self.preemptible_rules.is_preemptible(rule.name) for rule in job.rules
            ):
                raise WorkflowError(
                    "All grouped rules should be homogenously set as preemptible rules"
                    "(see Defining groups for execution in snakemake documentation)"
                )
        else:
            preemptible = self.preemptible_rules.is_preemptible(job.rule.name)

        # We add the size for the image itself (10 GB) to bootDiskSizeGb
        virtual_machine = {
            "machineType": smallest,
            "labels": {"app": "snakemake"},
            "bootDiskSizeGb": disk_gb + 10,
            "preemptible": preemptible,
        }

        # Add custom GCE VM configuration
        if self.network and self.subnetwork:
            virtual_machine["network"] = {
                "network": self.network,
                "usePrivateAddress": False,
                "subnetwork": self.subnetwork,
            }

        if self.service_account_email:
            virtual_machine["service_account"] = {
                "email": self.service_account_email,
                "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
            }

        # If the user wants gpus, add accelerators here
        if gpu_count:
            accelerator = self._get_accelerator(
                gpu_count, zone=selected["zone"], gpu_model=gpu_model
            )
            virtual_machine["accelerators"] = [
                {"type": accelerator["name"], "count": gpu_count}
            ]

        resources = {"regions": self.regions, "virtualMachine": virtual_machine}
        return resources

    def _get_accelerator(self, gpu_count, zone, gpu_model=None):
        """
        Get an appropriate accelerator for a GPU given a zone selection.
        Currently Google offers NVIDIA Tesla T4 (likely the best),
        NVIDIA P100, and the same T4 for a graphical workstation. Since
        this isn't a graphical workstation use case, we choose the
        accelerator that has >= to the maximumCardsPerInstace
        """
        if not gpu_count or gpu_count == 0:
            return

        accelerators = self._retry_request(
            self._compute_cli.acceleratorTypes().list(project=self.project, zone=zone)
        )

        # Filter down to those with greater than or equal to needed gpus
        keepers = {}
        for accelerator in accelerators.get("items", []):
            # Eliminate virtual workstations (vws) and models that don't match user preference
            if (gpu_model and accelerator["name"] != gpu_model) or accelerator[
                "name"
            ].endswith("vws"):
                continue

            if accelerator["maximumCardsPerInstance"] >= gpu_count:
                keepers[accelerator["name"]] = accelerator

        # If no matches available, exit early
        if not keepers:
            if gpu_model:
                raise WorkflowError(
                    "An accelerator in zone {zone} with model {model} cannot "
                    " be satisfied, so there are no options "
                    "available.".format(zone=zone, model=gpu_model)
                )
            else:
                raise WorkflowError(
                    "An accelerator in zone {zone} cannot be satisifed, so "
                    "there are no options available.".format(zone=zone)
                )

        # Find smallest (in future the user might have preference for the type)
        smallest = list(keepers.keys())[0]
        max_gpu = keepers[smallest]["maximumCardsPerInstance"]

        # This should usually return P-100, which would be preference (cheapest)
        for name, accelerator in keepers.items():
            if accelerator["maximumCardsPerInstance"] < max_gpu:
                smallest = name
                max_gpu = accelerator["maximumCardsPerInstance"]

        return keepers[smallest]
    
    def get_snakefile(self):
        assert os.path.exists(self.workflow.main_snakefile)
        return self.workflow.main_snakefile.removeprefix(self.workdir).strip(os.sep)
    
    def _set_workflow_sources(self):
        """
        We only add files from the working directory that are config related
        (e.g., the Snakefile or a config.yml equivalent), or checked into git.
        """
        self.workflow_sources = []

        for wfs in self.dag.get_sources():
            if os.path.isdir(wfs):
                for dirpath, dirnames, filenames in os.walk(wfs):
                    self.workflow_sources.extend(
                        [self.check_source_size(os.path.join(dirpath, f)) for f in filenames]
                    )
            else:
                self.workflow_sources.append(self.check_source_size(os.path.abspath(wfs)))

    def _generate_build_source_package(self):
        """
        In order for the instance to access the working directory in storage,
        we need to upload it. This file is cleaned up at the end of the run.
        We do this, and then obtain from the instance and extract.
        """
        # Workflow sources for cloud executor must all be under same workdir root
        for filename in self.workflow_sources:
            if self.workdir not in os.path.realpath(filename):
                raise WorkflowError(
                    "All source files must be present in the working directory, "
                    "{workdir} to be uploaded to a build package that respects "
                    "relative paths, but {filename} was found outside of this "
                    "directory. Please set your working directory accordingly, "
                    "and the path of your Snakefile to be relative to it.".format(
                        workdir=self.workdir, filename=filename
                    )
                )

        # We will generate a tar.gz package, renamed by hash
        tmpname = next(tempfile._get_candidate_names())
        targz = os.path.join(tempfile.gettempdir(), "snakemake-%s.tar.gz" % tmpname)
        tar = tarfile.open(targz, "w:gz")

        # Add all workflow_sources files
        for filename in self.workflow_sources:
            arcname = filename.replace(self.workdir + os.path.sep, "")
            tar.add(filename, arcname=arcname)

        tar.close()

        # Rename based on hash, in case user wants to save cache
        hasher = hashlib.sha256()
        hasher.update(open(targz, "rb").read())
        sha256 = hasher.hexdigest()
        hash_tar = os.path.join(
            self.workflow.persistence.aux_path, f"workdir-{sha256}.tar.gz"
        )

        # Only copy if we don't have it yet, clean up if we do
        if not os.path.exists(hash_tar):
            shutil.move(targz, hash_tar)
        else:
            os.remove(targz)

        # We will clean these all up at shutdown
        self._build_packages.add(hash_tar)

        return hash_tar

    def _upload_build_source_package(self, targz):
        """
        Given a .tar.gz created for a workflow, upload it to source/cache
        of Google storage, only if the blob doesn't already exist.
        """
        @retry.Retry(predicate=google_cloud_retry_predicate)
        def _upload():
            # Upload to temporary storage, only if doesn't exist
            self.pipeline_package = "source/cache/%s" % os.path.basename(targz)
            blob = self.bucket.blob(self.pipeline_package)
            self.logger.debug("build-package=%s" % self.pipeline_package)
            if not blob.exists():
                blob.upload_from_filename(targz, content_type="application/gzip")

        _upload()

    def _generate_log_action(self, job: ExecutorJobInterface):
        """generate an action to save the pipeline logs to storage."""
        # script should be changed to this when added to version control!
        # https://raw.githubusercontent.com/snakemake/snakemake/main/snakemake/executors/google_lifesciences_helper.py
        # Save logs from /google/logs/output to source/logs in bucket
        commands = [
            "/bin/bash",
            "-c",
            f"wget -O /gls.py https://raw.githubusercontent.com/snakemake/snakemake-executor-plugin-google-lifesciences/main/snakemake_executor_plugin_google_lifesciences/google_lifesciences_helper.py && chmod +x /gls.py && source activate snakemake || true && python /gls.py save {self.bucket.name} /google/logs {self.gs_logs}/{job.name}/jobid_{job.jobid}",
        ]

        # Always run the action to generate log output
        action = {
            "containerName": f"snakelog-{job.name}-{job.jobid}",
            "imageUri": self.container_image,
            "commands": commands,
            "labels": self._generate_pipeline_labels(job),
            "alwaysRun": True,
        }

        return action

    def _generate_job_action(self, job: ExecutorJobInterface):
        """
        Generate a single action to execute the job.
        """
        exec_job = self.format_job_exec(job)

        # The full command to download the archive, extract, and run
        # For snakemake bases, we must activate the conda environment, but
        # for custom images we must allow this to fail (hence || true)
        commands = [
            "/bin/bash",
            "-c",
            "mkdir -p /workdir && "
            "cd /workdir && "
            "wget -O /download.py "
            "https://raw.githubusercontent.com/snakemake/snakemake-executor-plugin-google-lifesciences/main/snakemake_executor_plugin_google_lifesciences/google_lifesciences_helper.py && "
            "chmod +x /download.py && "
            "source activate snakemake || true && "
            f"python /download.py download {self.bucket.name} {self.pipeline_package} "
            "/tmp/workdir.tar.gz && "
            f"tar -xzvf /tmp/workdir.tar.gz && {exec_job}",
        ]

        # We are only generating one action, one job per run
        # https://cloud.google.com/life-sciences/docs/reference/rest/v2beta/projects.locations.pipelines/run#Action
        action = {
            "containerName": f"snakejob-{job.name}-{job.jobid}",
            "imageUri": self.container_image,
            "commands": commands,
            "environment": self._generate_environment(),
            "labels": self._generate_pipeline_labels(job),
        }
        return action

    def _get_jobname(self, job: ExecutorJobInterface):
        # Use a dummy job name (human readable and also namespaced)
        return f"snakejob-{self.run_namespace}-{job.name}-{job.jobid}"

    def _generate_pipeline_labels(self, job: ExecutorJobInterface):
        """
        Generate basic labels to identify the job, namespace, and that
        snakemake is running the show!
        """
        jobname = self._get_jobname(job)
        labels = {"name": jobname, "app": "snakemake"}
        return labels

    def _generate_environment(self):
        """loop through envvars (keys to host environment) and add
        any that are requested for the container environment.
        """
        envvars = {}
        for key in self.envvars:
            try:
                envvars[key] = os.environ[key]
            except KeyError:
                continue

        # Warn the user that we cannot support secrets
        if envvars:
            self.logger.warning("This API does not support environment secrets.")
        return envvars

    def _generate_pipeline(self, job: ExecutorJobInterface):
        """
        Based on the job details, generate a google Pipeline object
        to pass to pipelines.run. This includes actions, resources,
        environment, and timeout.
        """
        # Generate actions (one per job step) and log saving action (runs no matter what) and resources
        resources = self._generate_job_resources(job)
        action = self._generate_job_action(job)
        log_action = self._generate_log_action(job)

        pipeline = {
            # Ordered list of actions to execute
            "actions": [action, log_action],
            # resources required for execution
            "resources": resources,
            # Technical question - difference between resource and action environment
            # For now we will set them to be the same.
            "environment": self._generate_environment(),
        }

        # "timeout": string in seconds (3.5s) is not included (defaults to 7 days)
        return pipeline

    def _get_services(self):
        """
        Use the Google Discovery Build to generate API clients
        for Life Sciences, and use the google storage python client
        for storage.
        """

        # Credentials may be exported to the environment or from a service account on a GCE VM instance.
        try:
            # oauth2client is deprecated, see: https://google-auth.readthedocs.io/en/master/oauth2client-deprecation.html
            # google.auth is replacement
            # not sure about scopes here. this cover all cloud services
            creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
        except google.auth.DefaultCredentialsError as ex:
            raise WorkflowError(ex)

        def build_request(http, *args, **kwargs):
            """
            See https://googleapis.github.io/google-api-python-client/docs/thread_safety.html
            """
            new_http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http())
            return googleapiclient.http.HttpRequest(new_http, *args, **kwargs)

        # Discovery clients for Google Cloud Storage and Life Sciences API
        # create authorized http for building services
        authorized_http = google_auth_httplib2.AuthorizedHttp(
            creds, http=httplib2.Http()
        )
        self._storage_cli = discovery_build(
            "storage",
            "v1",
            cache_discovery=False,
            requestBuilder=build_request,
            http=authorized_http,
        )
        self._compute_cli = discovery_build(
            "compute",
            "v1",
            cache_discovery=False,
            requestBuilder=build_request,
            http=authorized_http,
        )
        self._api = discovery_build(
            "lifesciences",
            "v2beta",
            cache_discovery=False,
            requestBuilder=build_request,
            http=authorized_http,
        )
        self._bucket_service = storage.Client()

    def _get_bucket(self):
        """
        Get a connection to the storage bucket (self.bucket) and exit
        if the name is taken or otherwise invalid.

        Parameters
        ==========
        workflow: the workflow object to derive the prefix from
        """

        # TODO this does not work if the remote is used without default_remote_prefix
        # Hold path to requested subdirectory and main bucket
        bucket_name = self.workflow.storage_settings.default_remote_prefix.split("/")[0]
        self.gs_subdir = re.sub(
            f"^{bucket_name}/", "", self.workflow.storage_settings.default_remote_prefix
        )
        self.gs_logs = os.path.join(self.gs_subdir, "google-lifesciences-logs")

        # Case 1: The bucket already exists
        try:
            self.bucket = self._bucket_service.get_bucket(bucket_name)

        # Case 2: The bucket needs to be created
        except google.cloud.exceptions.NotFound:
            self.bucket = self._bucket_service.create_bucket(bucket_name)

        # Case 2: The bucket name is already taken
        except Exception as ex:
            self.logger.error(
                "Cannot get or create {} (exit code {}):\n{}".format(
                    bucket_name, ex.returncode, ex.output.decode()
                )
            )
            raise WorkflowError(
                "Cannot get or create {} (exit code {}):\n{}".format(
                    bucket_name, ex.returncode, ex.output.decode()
                ),
                ex
            )

        self.logger.debug("bucket=%s" % self.bucket.name)
        self.logger.debug("subdir=%s" % self.gs_subdir)
        self.logger.debug("logs=%s" % self.gs_logs)

    def _set_location(self, location=None):
        """
        The location is where the Google Life Sciences API is located.
        This can be meaningful if the requester has data residency
        requirements or multi-zone needs. To determine this value,
        we first use the locations API to determine locations available,
        and then compare them against:

        1. user specified location or prefix
        2. regions having the same prefix
        3. if cannot be satisifed, we throw an error.
        """
        # Derive available locations
        # See https://cloud.google.com/life-sciences/docs/concepts/locations
        locations = (
            self._api.projects()
            .locations()
            .list(name=f"projects/{self.project}")
            .execute()
        )

        locations = {x["locationId"]: x["name"] for x in locations.get("locations", [])}

        # Alert the user about locations available
        self.logger.debug("locations-available:\n%s" % "\n".join(locations))

        # If no locations, there is something wrong
        if not locations:
            raise WorkflowError("No locations found for Google Life Sciences API.")

        # First pass, attempt to match the user-specified location (or prefix)
        if location:
            if location in locations:
                self.location = locations[location]
                return

            # It could be that a prefix was provided
            for contender in locations:
                if contender.startswith(location):
                    self.location = locations[contender]
                    return

            # If we get here and no match, alert user.
            raise WorkflowError(
                "Location or prefix requested %s is not available." % location
            )

        # If we get here, we need to select location from regions
        for region in self.regions:
            if region in locations:
                self.location = locations[region]
                return

        # If we get here, choose based on prefix
        prefixes = set([r.split("-")[0] for r in self.regions])
        regexp = "^(%s)" % "|".join(prefixes)
        for location in locations:
            if re.search(regexp, location):
                self.location = locations[location]
                return

        # If we get here, total failure of finding location
        raise WorkflowError(
            " No locations available for regions!"
            " Please specify a location with --google-lifesciences-location "
            " or extend --google-lifesciences-regions to find a Life Sciences location."
        )
    
    def check_source_size(self, filename, warning_size_gb=0.2):
        """A helper function to check the filesize, and return the file
        to the calling function Additionally, given that we encourage these
        packages to be small, we set a warning at 200MB (0.2GB).
        """
        gb = bytesto(os.stat(filename).st_size, "g")
        if gb > warning_size_gb:
            self.logger.warning(
                f"File {filename} (size {gb} GB) is greater than the {warning_size_gb} GB "
                f"suggested size. Consider uploading larger files to storage first."
            )
        return filename
