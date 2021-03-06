"""Workflow to ingest and process EM data.

--Settings--

Command line json:
{
    email: foo@bar # where to send results (currently disabled)
    createRawPyramid: True # create raw scale pyramid in addition to jpeg (True is default)
    image: "template%d.png", # template name
    minz: 0, # first slice
    maxz: 50, # last slice
    source: bucket_name # location of stored pngs
    downsample_factor: 4 # how much to downsample before aligning
    "id": "name of dataset"
}

Input: images in a source/raw/*.png

Environment: If testing locally without data, set AIRFLOW_TEST_MODE=1

Airflow Configuration:

Setup a pool with  workers for lightweight http requests
called "http_requests" to be equal to the WORKER_POOL.

Configure email smptp as appropriate (currently disabled)

Conn id:

* ALIGN_CLOUD_RUN (str): http address
* IMG_WRITE (str): http address

Airflow Variables:

    Ideally set "emprocess_version" to be the current version to make sure 
    old dag versions are not run.

"""


# large http requests are grouped into pool
WORKER_POOLS = [128, 64, 32, 4, 1]

from airflow.models import DAG
from airflow.operators.python_operator import PythonOperator, BranchPythonOperator, ShortCircuitOperator
from airflow.contrib.hooks.gcs_hook import GoogleCloudStorageHook
from datetime import datetime
from airflow.utils.trigger_rule import TriggerRule
#from airflow.operators.email_operator import EmailOperator
from airflow.operators.bash_operator import BashOperator
from airflow.operators.subdag_operator import SubDagOperator
from airflow.models import Variable
from airflow import AirflowException
import subprocess

import logging
import json

# custom local dependencies
from emprocess import align, pyramid

# check if in testing mode
import os
TEST_MODE_ENV = os.environ.get("AIRFLOW_TEST_MODE", None)
TEST_MODE = False
if TEST_MODE_ENV is not None:
    TEST_MODE = True



"""Version of dag.

For very small non-functional change, do not modify.   For small changes for performance
and optimization that don't impact DAG or results greatly, modify subversion.

For task 'additions' and bigger optimization
changes, minor version change.  For very large DAG changes, major version number change.
Both minor and major version changes will reseult in a new DAG workflow.

The initial operator should attempt to print out any necessary provenance
so that it is in the log (such as command line options).  Somehow provide
some version information for dependencies (preferably automatically).

The old dags can be cached (though shouldn't be run) by Airflow.
"""

VERSION = "0.1"
SUBVERSION = "1"
SHARD_SIZE = 1024 
START_DATE = datetime(2020, 4, 21) # date when these workflows became relevant (mostly legacy for scheduling work)

for WORKER_POOL in WORKER_POOLS:

    DAG_NAME = f'emprocess_width{WORKER_POOL}_v{VERSION}'

    # each dagrun is executed once and at time of submission
    DEFAULT_ARGS = {
            "owner": "airflow",
            "retries": 1,
            "start_date": START_DATE,
            #"email_on_failure": True,
            #"email_on_retry": True,
            }

    dag = DAG(
            DAG_NAME,
            default_args=DEFAULT_ARGS,
            description="workflow to ingest, align, and process EM data",
            schedule_interval=None,
            )

    # set to global
    globals()[DAG_NAME] = dag
   
    def validate_params(**kwargs):
        """Check that img name, google bucket, and image range is specified.
        """

        logging.info(f"Version({VERSION}) Sub-version({SUBVERSION})")
        logging.info(f"Chunk size: {SHARD_SIZE})")

        # check if runtime version matches what is in airflow (this is a relevant
        # check if caching is enabled and old workflow are around but no longer supported in source).
        # (might be unnecessary)
        version = Variable.get("emprocess_version", VERSION)
        if version != VERSION:
            raise AirflowException("executing emprocess version {version} is not supported")

        # check if email is provided
        email_addr = kwargs['dag_run'].conf.get('email')
        if email_addr is None:
            raise AirflowException("no email provided")

        logging.info(f"Email provided: {email_addr}")

        # check raw pyrmaid config
        if kwargs['dag_run'].conf.get('createRawPyramid', True):
            logging.info("Enables raw pyramid creation")
        else:
            logging.info("Disable raw pyramid creation")

        # check resolution
        res =  kwargs['dag_run'].conf.get('resolution', 8)
        logging.info(f"Resolution: {res}")

        # log downsample factor
        downsample_factor = kwargs['dag_run'].conf.get('downsample_factor', 1)
        logging.info(f"Downsample factor: {downsample_factor}")

        # format string for image name
        name = kwargs['dag_run'].conf.get('image')
        if name is None:
            raise AirflowException("no image exists")

        # check for [minz, maxz] values
        minz = kwargs['dag_run'].conf.get('minz')
        if minz is None:
            raise AirflowException("no minz exists")

        maxz = kwargs['dag_run'].conf.get('maxz')
        if maxz is None:
            raise AirflowException("no maxz exists")

        if minz > maxz:
            raise AirflowException("no maxz should be greater than minz")

        # location of storage (i.e., storage bucket name)
        location = kwargs['dag_run'].conf.get('source')
        if location is None:
            raise AirflowException("no location exists")
        
    # validate parameters
    validate_t = PythonOperator(
            task_id="validate",
            provide_context=True,
            python_callable=validate_params, 
            dag=dag,
            )

    def create_env(run_id, **context):
        """Run id should be some random UUID.
        """
        
        ghook = GoogleCloudStorageHook() # uses default gcp connection
        bucket_name = context["dag_run"].conf.get('source')
        project_id = context["dag_run"].conf.get("project_id")
        if not TEST_MODE:
            """
            # _process bucket could already exist
            try:
                subprocess.check_output([f"gsutil mb -p {project_id} -l US-EAST4 -b on gs://{bucket_name + '_process'}"], shell=True).decode()
            except Exception:
                pass

            # other buckets should not have been created before

            # this data can be used for chunk-based image processing)
            try:
                subprocess.check_output([f"gsutil mb -p {project_id} -l US-EAST4 -b on gs://{bucket_name + '_chunk_' + run_id}"], shell=True).decode()
            except Exception:
                pass

            # will be auto deleted
            try:
                subprocess.check_output([f"gsutil mb -p {project_id} -l US-EAST4 -b on gs://{bucket_name + '_tmp_' + run_id}"], shell=True).decode()
            except Exception:
                pass

            # will be made public readable
            try:
                subprocess.check_output([f"gsutil mb -p {project_id} -l US-EAST4 -b on gs://{bucket_name + '_ng_' + run_id}"], shell=True).decode()
            except Exception:
                pass
            """

            # interface does not support enabling uniform IAM. 
            # create bucket for configs (ignore if it already existss
            try:
                ghook.create_bucket(bucket_name=bucket_name + "_process", project_id=project_id, storage_class="REGIONAL", location="US-EAST4")
            except AirflowException as e:
                # ignore if the erorr is the bucket exists
                if not str(e).startswith("409"):
                    raise

            # other buckets should not have been created before

            # this data can be used for chunk-based image processing)
            ghook.create_bucket(bucket_name=bucket_name + "_chunk_" + run_id, project_id=project_id, storage_class="REGIONAL", location="US-EAST4")
            
            # will be auto deleted
            ghook.create_bucket(bucket_name=bucket_name + "_tmp_" + run_id, project_id=project_id) #, storage_class="REGIONAL", location="US-EAST4")
            
            # will be made public readable
            ghook.create_bucket(bucket_name=bucket_name + "_ng_" + run_id, project_id=project_id, storage_class="REGIONAL", location="US-EAST4")

            # dump configuration
            client = ghook.get_conn()
            source = context["dag_run"].conf.get("source")
            bucket = client.bucket(source + "_process")
            blob = bucket.blob(blob_name=f"{context['dag_run'].run_id}/init.json")

            data = context["dag_run"].conf
            data["execution_date"] = str(context.get("execution_date")) 
            data = json.dumps(data)
            blob.upload_from_string(data) 



    # create UUID for dag run and necessary gbuckets
    create_env_t = PythonOperator(
            task_id="create_env",
            provide_context=True,
            python_callable=create_env,
            op_kwargs={"run_id": "{{run_id}}"},
            dag=dag,
            )
    # expects dag run configruation with "image", "minz", "maxz", "source", "project", and "downsample_factor"
    align_start_t, align_end_t = align.align_dataset_psubdag(dag, DAG_NAME+".align", WORKER_POOL,
            "http_requests", TEST_MODE, SHARD_SIZE)

    
    # expects dag run configruation with "image", "minz", "maxz", "source"
    ngingest_start_t, ngingest_end_t = pyramid.export_dataset_psubdag(dag, DAG_NAME+".ngingest", WORKER_POOL,
            align_end_t.task_id, "http_requests", TEST_MODE, SHARD_SIZE)

    # pull xcom from a subdag to see if data was written
    def iswritten(value, **context):
        #value = context['task_instance'].xcom_pull(dag_id=f"{DAG_NAME}.align", task_ids="write_align")
        #value = context['task_instance'].xcom_pull(task_ids=align_end_t.task_id, key="bbox")
        #logging.info(align_end_t.task_id)
        if value is not None:
            return value
        return False

    # conditional for successful alignment
    isaligned_t = ShortCircuitOperator(
        task_id='iswritten',
        python_callable=iswritten,
        trigger_rule=TriggerRule.ALL_DONE,
        op_kwargs={"value": f"{{{{ task_instance.xcom_pull(task_ids='{align_end_t.task_id}') }}}}"},
        provide_context=True,
        dag=dag)

    # delete source_{ds_nodash}/(*.png) (run if align_t succeeds and ngingest finishes) -- let it survive for 1 day in case there are re-runs and the same policy is still in effect
    lifecycle_config = {
                        "lifecycle": {
                            "rule": [
                                {
                                    "action": {"type": "Delete"},
                                    "condition": {
                                        "age": 5
                                        }
                                }
                                ]
                        }
                        }
    commands = f"echo '{json.dumps(lifecycle_config)}' > life.json;\n"
    if not TEST_MODE:
        commands += "gsutil lifecycle set life.json gs://{{ dag_run.conf['source'] }}_tmp_{{ run_id }};\n"
    commands += "rm life.json;"

    cleanup_t = BashOperator(
                    task_id="cleanup_images",
                    bash_command=commands,
                    dag=dag,
                )


    """
    # notify user
    notify_t = EmailOperator(
            task_id="notify",
            to="{{ dag_run.conf['email'] }}",
            subject=f"airflow:{DAG_NAME}",
            html_content="Job finished.  View on neuroglancer (source = precomputed://gs://{{ dag_run.conf['source'] }}_ng_{{ run_id }}/neuroglancer/jpeg)",
            dag=dag
    )
    """

    read_config = [
                {
                  "origin": ["*"],
                  "responseHeader": ["Content-Length", "Content-Type", "Date", "Range", "Server", "Transfer-Encoding", "X-GUploader-UploadID", "X-Google-Trace", "Access-Control-Allow-Credentials"], 
                  "method": ["GET", "HEAD", "OPTIONS", "POST"],
                  "maxAgeSeconds": 3600
                }
                ]

    read_commands = f"echo '{json.dumps(read_config)}' > read.json;\n"
    if not TEST_MODE:
        read_commands += "gsutil iam ch allUsers:objectViewer gs://{{ dag_run.conf['source'] }}_ng_{{ run_id }}; gsutil cors set read.json gs://{{ dag_run.conf['source'] }}_ng_{{ run_id }};\n"
    read_commands += "rm read.json;"

    set_public_read_t = BashOperator(
                    task_id="set_public_read",
                    bash_command=read_commands,
                    dag=dag,
                )

    def write_status(**context):
        # test mode disable
        if not TEST_MODE:
            # write config and time stamp
            ghook = GoogleCloudStorageHook() # uses default gcp connection
            client = ghook.get_conn()
            source = context["dag_run"].conf.get("source")
            bucket = client.bucket(source + "_process")
            blob = bucket.blob(blob_name=f"{context['dag_run'].run_id}/complete.json")
            project_id = context["dag_run"].conf.get("project_id")

            data = context["dag_run"].conf
            data["execution_date"] = str(context.get("execution_date")) 
            data = json.dumps(data)
            blob.upload_from_string(data) 

    # write results to gbucket
    write_status_t = PythonOperator(
        task_id="write_status",
        python_callable=write_status,
        provide_context=True,
        dag=dag,
    )

    # cleanup is triggered if alignment completes properly
    validate_t >> create_env_t >> align_start_t
    align_end_t >> ngingest_start_t
    [align_end_t, ngingest_end_t] >> isaligned_t >> cleanup_t 
    #[ngingest_end_t, cleanup_t] >> set_public_read_t >> notify_t >> write_status_t
    [ngingest_end_t, cleanup_t] >> set_public_read_t >> write_status_t


