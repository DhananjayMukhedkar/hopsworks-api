#
#   Copyright 2020 Logical Clocks AB
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
from __future__ import annotations

import json
import logging
import math
import numbers
import os
import random
import re
import sys
import uuid
import warnings
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Literal,
    Optional,
    Tuple,
    Union,
)

from hsfs.core.type_systems import (
    cast_column_to_offline_type,
    cast_column_to_online_type,
)


if TYPE_CHECKING:
    import great_expectations

import boto3
import hsfs
import numpy as np
import pandas as pd
import polars as pl
import pyarrow as pa
from botocore.response import StreamingBody
from hopsworks_common import client
from hopsworks_common.client.exceptions import FeatureStoreException
from hsfs import (
    feature,
    feature_view,
    transformation_function,
    util,
)
from hsfs import storage_connector as sc
from hsfs.constructor import query
from hsfs.core import (
    arrow_flight_client,
    dataset_api,
    feature_group_api,
    feature_view_api,
    ingestion_job_conf,
    job,
    job_api,
    kafka_engine,
    statistics_api,
    storage_connector_api,
    training_dataset_api,
    training_dataset_job_conf,
    transformation_function_engine,
)
from hsfs.core.constants import (
    HAS_AIOMYSQL,
    HAS_ARROW,
    HAS_GREAT_EXPECTATIONS,
    HAS_PANDAS,
    HAS_SQLALCHEMY,
)
from hsfs.core.feature_view_engine import FeatureViewEngine
from hsfs.core.vector_db_client import VectorDbClient
from hsfs.decorators import uses_great_expectations
from hsfs.feature_group import ExternalFeatureGroup, FeatureGroup
from hsfs.training_dataset import TrainingDataset
from hsfs.training_dataset_feature import TrainingDatasetFeature
from hsfs.training_dataset_split import TrainingDatasetSplit


if HAS_GREAT_EXPECTATIONS:
    import great_expectations

if HAS_ARROW:
    from hsfs.core.type_systems import PYARROW_HOPSWORKS_DTYPE_MAPPING
if HAS_AIOMYSQL and HAS_SQLALCHEMY:
    from hsfs.core import util_sql

if HAS_SQLALCHEMY:
    from sqlalchemy import sql

if HAS_PANDAS:
    from hsfs.core.type_systems import convert_pandas_dtype_to_offline_type

_logger = logging.getLogger(__name__)


class Engine:
    def __init__(self) -> None:
        _logger.debug("Initialising Python Engine...")
        self._dataset_api: dataset_api.DatasetApi = dataset_api.DatasetApi()
        self._job_api: job_api.JobApi = job_api.JobApi()
        self._feature_group_api: feature_group_api.FeatureGroupApi = (
            feature_group_api.FeatureGroupApi()
        )
        self._storage_connector_api: storage_connector_api.StorageConnectorApi = (
            storage_connector_api.StorageConnectorApi()
        )

        # cache the sql engine which contains the connection pool
        self._mysql_online_fs_engine = None
        _logger.info("Python Engine initialized.")

    def sql(
        self,
        sql_query: str,
        feature_store: str,
        online_conn: Optional[sc.JdbcConnector],
        dataframe_type: str,
        read_options: Optional[Dict[str, Any]],
        schema: Optional[List[feature.Feature]] = None,
    ) -> Union[pd.DataFrame, pl.DataFrame]:
        if not online_conn:
            return self._sql_offline(
                sql_query,
                dataframe_type,
                schema,
                arrow_flight_config=read_options.get("arrow_flight_config", {})
                if read_options
                else {},
            )
        else:
            return self._jdbc(
                sql_query, online_conn, dataframe_type, read_options, schema
            )

    def is_flyingduck_query_supported(
        self, query: "query.Query", read_options: Optional[Dict[str, Any]] = None
    ) -> bool:
        return arrow_flight_client.is_query_supported(query, read_options or {})

    def _validate_dataframe_type(self, dataframe_type: str):
        if not isinstance(dataframe_type, str) or dataframe_type.lower() not in [
            "pandas",
            "polars",
            "numpy",
            "python",
            "default",
        ]:
            raise FeatureStoreException(
                f'dataframe_type : {dataframe_type} not supported. Possible values are "default", "pandas", "polars", "numpy" or "python"'
            )

    def _sql_offline(
        self,
        sql_query: str,
        dataframe_type: str,
        schema: Optional[List["feature.Feature"]] = None,
        arrow_flight_config: Optional[Dict[str, Any]] = None,
    ) -> Union[pd.DataFrame, pl.DataFrame]:
        self._validate_dataframe_type(dataframe_type)
        if isinstance(sql_query, dict) and "query_string" in sql_query:
            result_df = util.run_with_loading_animation(
                "Reading data from Hopsworks, using Hopsworks Feature Query Service",
                arrow_flight_client.get_instance().read_query,
                sql_query,
                arrow_flight_config or {},
                dataframe_type,
            )
        else:
            raise ValueError(
                "Reading data with Hive is not supported when using hopsworks client version >= 4.0"
            )
        if schema:
            result_df = Engine.cast_columns(result_df, schema)
        return self._return_dataframe_type(result_df, dataframe_type)

    def _jdbc(
        self,
        sql_query: str,
        connector: sc.JdbcConnector,
        dataframe_type: str,
        read_options: Optional[Dict[str, Any]],
        schema: Optional[List[feature.Feature]] = None,
    ) -> Union[pd.DataFrame, pl.DataFrame]:
        self._validate_dataframe_type(dataframe_type)
        if self._mysql_online_fs_engine is None:
            self._mysql_online_fs_engine = util_sql.create_mysql_engine(
                connector,
                (
                    client._is_external()
                    if "external" not in read_options
                    else read_options["external"]
                ),
            )
        with self._mysql_online_fs_engine.connect() as mysql_conn:
            if "sqlalchemy" in str(type(mysql_conn)):
                sql_query = sql.text(sql_query)
            if dataframe_type.lower() == "polars":
                result_df = pl.read_database(sql_query, mysql_conn)
            else:
                result_df = pd.read_sql(sql_query, mysql_conn)
            if schema:
                result_df = Engine.cast_columns(result_df, schema, online=True)
        return self._return_dataframe_type(result_df, dataframe_type)

    def read(
        self,
        storage_connector: sc.StorageConnector,
        data_format: str,
        read_options: Optional[Dict[str, Any]],
        location: Optional[str],
        dataframe_type: Literal["polars", "pandas", "default"],
    ) -> Union[pd.DataFrame, pl.DataFrame]:
        if not data_format:
            raise FeatureStoreException("data_format is not specified")

        if storage_connector.type == storage_connector.HOPSFS:
            df_list = self._read_hopsfs(
                location, data_format, read_options, dataframe_type
            )
        elif storage_connector.type == storage_connector.S3:
            df_list = self._read_s3(
                storage_connector, location, data_format, dataframe_type
            )
        else:
            raise NotImplementedError(
                "{} Storage Connectors for training datasets are not supported yet for external environments.".format(
                    storage_connector.type
                )
            )
        if dataframe_type.lower() == "polars":
            # Below check performed since some files materialized when creating training data are empty
            # If empty dataframe is in df_list then polars cannot concatenate df_list due to schema mismatch
            # However if the entire split contains only empty files which can occur when the data size is very small then one of the empty dataframe is return so that the column names can be accessed.
            non_empty_df_list = [df for df in df_list if not df.is_empty()]
            if non_empty_df_list:
                return self._return_dataframe_type(
                    pl.concat(non_empty_df_list), dataframe_type=dataframe_type
                )
            else:
                return df_list[0]
        else:
            return self._return_dataframe_type(
                pd.concat(df_list, ignore_index=True), dataframe_type=dataframe_type
            )

    def _read_pandas(self, data_format: str, obj: Any) -> pd.DataFrame:
        if data_format.lower() == "csv":
            return pd.read_csv(obj)
        elif data_format.lower() == "tsv":
            return pd.read_csv(obj, sep="\t")
        elif data_format.lower() == "parquet" and isinstance(obj, StreamingBody):
            return pd.read_parquet(BytesIO(obj.read()))
        elif data_format.lower() == "parquet":
            return pd.read_parquet(obj)
        else:
            raise TypeError(
                "{} training dataset format is not supported to read as pandas dataframe.".format(
                    data_format
                )
            )

    def _read_polars(
        self, data_format: Literal["csv", "tsv", "parquet"], obj: Any
    ) -> pl.DataFrame:
        if data_format.lower() == "csv":
            return pl.read_csv(obj)
        elif data_format.lower() == "tsv":
            return pl.read_csv(obj, separator="\t")
        elif data_format.lower() == "parquet" and isinstance(obj, StreamingBody):
            return pl.read_parquet(BytesIO(obj.read()), use_pyarrow=True)
        elif data_format.lower() == "parquet":
            return pl.read_parquet(obj, use_pyarrow=True)
        else:
            raise TypeError(
                "{} training dataset format is not supported to read as polars dataframe.".format(
                    data_format
                )
            )

    def _is_metadata_file(self, path):
        return Path(path).stem.startswith("_")

    def _read_hopsfs(
        self,
        location: str,
        data_format: str,
        read_options: Optional[Dict[str, Any]] = None,
        dataframe_type: str = "default",
    ) -> List[Union[pd.DataFrame, pl.DataFrame]]:
        return self._read_hopsfs_remote(
            location, data_format, read_options or {}, dataframe_type
        )

    # This read method uses the Hopsworks REST APIs or Flyingduck Server
    # To read the training dataset content, this to allow users to read Hopsworks training dataset from outside
    def _read_hopsfs_remote(
        self,
        location: str,
        data_format: str,
        read_options: Optional[Dict[str, Any]] = None,
        dataframe_type: str = "default",
    ) -> List[Union[pd.DataFrame, pl.DataFrame]]:
        total_count = 10000
        offset = 0
        df_list = []
        if read_options is None:
            read_options = {}

        while offset < total_count:
            total_count, inode_list = self._dataset_api.list_files(
                location, offset, 100
            )

            for inode in inode_list:
                if not self._is_metadata_file(inode.path):
                    if arrow_flight_client.is_data_format_supported(
                        data_format, read_options
                    ):
                        arrow_flight_config = read_options.get("arrow_flight_config")
                        df = arrow_flight_client.get_instance().read_path(
                            inode.path,
                            arrow_flight_config,
                            dataframe_type=dataframe_type,
                        )
                    else:
                        content_stream = self._dataset_api.read_content(inode.path)
                        if dataframe_type.lower() == "polars":
                            df = self._read_polars(
                                data_format, BytesIO(content_stream.content)
                            )
                        else:
                            df = self._read_pandas(
                                data_format, BytesIO(content_stream.content)
                            )

                    df_list.append(df)
                offset += 1

        return df_list

    def _read_s3(
        self,
        storage_connector: sc.S3Connector,
        location: str,
        data_format: str,
        dataframe_type: str = "default",
    ) -> List[Union[pd.DataFrame, pl.DataFrame]]:
        # get key prefix
        path_parts = location.replace("s3://", "").split("/")
        _ = path_parts.pop(0)  # pop first element -> bucket

        prefix = "/".join(path_parts)

        if storage_connector.session_token is not None:
            s3 = boto3.client(
                "s3",
                aws_access_key_id=storage_connector.access_key,
                aws_secret_access_key=storage_connector.secret_key,
                aws_session_token=storage_connector.session_token,
            )
        else:
            s3 = boto3.client(
                "s3",
                aws_access_key_id=storage_connector.access_key,
                aws_secret_access_key=storage_connector.secret_key,
            )

        df_list = []
        object_list = {"is_truncated": True}
        while object_list.get("is_truncated", False):
            if "NextContinuationToken" in object_list:
                object_list = s3.list_objects_v2(
                    Bucket=storage_connector.bucket,
                    Prefix=prefix,
                    MaxKeys=1000,
                    ContinuationToken=object_list["NextContinuationToken"],
                )
            else:
                object_list = s3.list_objects_v2(
                    Bucket=storage_connector.bucket,
                    Prefix=prefix,
                    MaxKeys=1000,
                )

            for obj in object_list["Contents"]:
                if not self._is_metadata_file(obj["Key"]) and obj["Size"] > 0:
                    obj = s3.get_object(
                        Bucket=storage_connector.bucket,
                        Key=obj["Key"],
                    )
                    if dataframe_type.lower() == "polars":
                        df_list.append(self._read_polars(data_format, obj["Body"]))
                    else:
                        df_list.append(self._read_pandas(data_format, obj["Body"]))
        return df_list

    def read_options(
        self, data_format: Optional[str], provided_options: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        return provided_options or {}

    def read_stream(
        self,
        storage_connector: sc.StorageConnector,
        message_format: Any,
        schema: Any,
        options: Optional[Dict[str, Any]],
        include_metadata: bool,
    ) -> Any:
        raise NotImplementedError(
            "Streaming Sources are not supported for pure Python Environments."
        )

    def show(
        self,
        sql_query: str,
        feature_store: str,
        n: int,
        online_conn: sc.JdbcConnector,
        read_options: Optional[Dict[str, Any]] = None,
    ) -> Union[pd.DataFrame, pl.DataFrame]:
        return self.sql(
            sql_query, feature_store, online_conn, "default", read_options or {}
        ).head(n)

    def read_vector_db(
        self,
        feature_group: "hsfs.feature_group.FeatureGroup",
        n: int = None,
        dataframe_type: str = "default",
    ) -> Union[pd.DataFrame, pl.DataFrame, np.ndarray, List[List[Any]]]:
        dataframe_type = dataframe_type.lower()
        self._validate_dataframe_type(dataframe_type)

        results = VectorDbClient.read_feature_group(feature_group, n)
        feature_names = [f.name for f in feature_group.features]
        if dataframe_type == "polars":
            df = pl.DataFrame(results, schema=feature_names)
        else:
            df = pd.DataFrame(results, columns=feature_names, index=None)
        return self._return_dataframe_type(df, dataframe_type)

    def register_external_temporary_table(
        self, external_fg: ExternalFeatureGroup, alias: str
    ) -> None:
        # No op to avoid query failure
        pass

    def register_hudi_temporary_table(
        self,
        hudi_fg_alias: "hsfs.constructor.hudi_feature_group_alias.HudiFeatureGroupAlias",
        feature_store_id: int,
        feature_store_name: str,
        read_options: Optional[Dict[str, Any]],
    ) -> None:
        if hudi_fg_alias and (
            hudi_fg_alias.left_feature_group_end_timestamp is not None
            or hudi_fg_alias.left_feature_group_start_timestamp is not None
        ):
            raise FeatureStoreException(
                "Incremental queries are not supported in the python client."
                + " Read feature group without timestamp to retrieve latest snapshot or switch to "
                + "environment with Spark Engine."
            )

    def profile_by_spark(
        self,
        metadata_instance: Union[
            FeatureGroup,
            ExternalFeatureGroup,
            feature_view.FeatureView,
            TrainingDataset,
        ],
    ) -> job.Job:
        stat_api = statistics_api.StatisticsApi(
            metadata_instance.feature_store_id, metadata_instance.ENTITY_TYPE
        )
        job = stat_api.compute(metadata_instance)
        print(
            "Statistics Job started successfully, you can follow the progress at \n{}".format(
                util.get_job_url(job.href)
            )
        )

        job._wait_for_job()
        return job

    def profile(
        self,
        df: Union[pd.DataFrame, pl.DataFrame],
        relevant_columns: List[str],
        correlations: Any,
        histograms: Any,
        exact_uniqueness: bool = True,
    ) -> str:
        # TODO: add statistics for correlations, histograms and exact_uniqueness
        if isinstance(df, pl.DataFrame) or isinstance(df, pl.dataframe.frame.DataFrame):
            arrow_schema = df.to_arrow().schema
        else:
            arrow_schema = pa.Schema.from_pandas(df, preserve_index=False)

        # parse timestamp columns to string columns
        for field in arrow_schema:
            if not (
                pa.types.is_null(field.type)
                or pa.types.is_list(field.type)
                or pa.types.is_large_list(field.type)
                or pa.types.is_struct(field.type)
            ) and PYARROW_HOPSWORKS_DTYPE_MAPPING[field.type] in ["timestamp", "date"]:
                if isinstance(df, pl.DataFrame) or isinstance(
                    df, pl.dataframe.frame.DataFrame
                ):
                    df = df.with_columns(pl.col(field.name).cast(pl.String))
                else:
                    df[field.name] = df[field.name].astype(str)

        if relevant_columns is None or len(relevant_columns) == 0:
            stats = df.describe().to_dict()
            relevant_columns = df.columns
        else:
            target_cols = [col for col in df.columns if col in relevant_columns]
            stats = df[target_cols].describe().to_dict()
        # df.describe() does not compute stats for all col types (e.g., string)
        # we need to compute stats for the rest of the cols iteratively
        missing_cols = list(set(relevant_columns) - set(stats.keys()))
        for col in missing_cols:
            stats[col] = df[col].describe().to_dict()
        final_stats = []
        for col in relevant_columns:
            if isinstance(df, pl.DataFrame) or isinstance(
                df, pl.dataframe.frame.DataFrame
            ):
                stats[col] = dict(zip(stats["statistic"], stats[col]))
            # set data type
            arrow_type = arrow_schema.field(col).type
            if (
                pa.types.is_null(arrow_type)
                or pa.types.is_list(arrow_type)
                or pa.types.is_large_list(arrow_type)
                or pa.types.is_struct(arrow_type)
                or PYARROW_HOPSWORKS_DTYPE_MAPPING[arrow_type]
                in ["timestamp", "date", "binary", "string"]
            ):
                dataType = "String"
            elif PYARROW_HOPSWORKS_DTYPE_MAPPING[arrow_type] in ["float", "double"]:
                dataType = "Fractional"
            elif PYARROW_HOPSWORKS_DTYPE_MAPPING[arrow_type] in ["int", "bigint"]:
                dataType = "Integral"
            elif PYARROW_HOPSWORKS_DTYPE_MAPPING[arrow_type] == "boolean":
                dataType = "Boolean"
            else:
                print(
                    "Data type could not be inferred for column '"
                    + col.split(".")[-1]
                    + "'. Defaulting to 'String'",
                    file=sys.stderr,
                )
                dataType = "String"

            stat = self._convert_pandas_statistics(stats[col], dataType)
            stat["isDataTypeInferred"] = "false"
            stat["column"] = col.split(".")[-1]
            stat["completeness"] = 1

            final_stats.append(stat)

        return json.dumps(
            {"columns": final_stats},
        )

    def _convert_pandas_statistics(
        self, stat: Dict[str, Any], dataType: str
    ) -> Dict[str, Any]:
        # For now transformation only need 25th, 50th, 75th percentiles
        # TODO: calculate properly all percentiles
        content_dict = {"dataType": dataType}
        if "count" in stat:
            content_dict["count"] = stat["count"]
        if not dataType == "String":
            if "25%" in stat:
                percentiles = [0] * 100
                percentiles[24] = stat["25%"]
                percentiles[49] = stat["50%"]
                percentiles[74] = stat["75%"]
                content_dict["approxPercentiles"] = percentiles
            if "mean" in stat:
                content_dict["mean"] = stat["mean"]
            if "mean" in stat and "count" in stat:
                if isinstance(stat["mean"], numbers.Number):
                    content_dict["sum"] = stat["mean"] * stat["count"]
            if "max" in stat:
                content_dict["maximum"] = stat["max"]
            if "std" in stat and not pd.isna(stat["std"]):
                content_dict["stdDev"] = stat["std"]
            if "min" in stat:
                content_dict["minimum"] = stat["min"]

        return content_dict

    def validate(
        self, dataframe: pd.DataFrame, expectations: Any, log_activity: bool = True
    ) -> None:
        raise NotImplementedError(
            "Deequ data validation is only available with Spark Engine. Use validate_with_great_expectations"
        )

    @uses_great_expectations
    def validate_with_great_expectations(
        self,
        dataframe: Union[pl.DataFrame, pd.DataFrame],
        expectation_suite: great_expectations.core.ExpectationSuite,
        ge_validate_kwargs: Optional[Dict[Any, Any]] = None,
    ) -> great_expectations.core.ExpectationSuiteValidationResult:
        # This conversion might cause a bottleneck in performance when using polars with greater expectations.
        # This patch is done becuase currently great_expecatations does not support polars, would need to be made proper when support added.
        if isinstance(dataframe, pl.DataFrame) or isinstance(
            dataframe, pl.dataframe.frame.DataFrame
        ):
            warnings.warn(
                "Currently Great Expectations does not support Polars dataframes. This operation will convert to Pandas dataframe that can be slow.",
                util.FeatureGroupWarning,
                stacklevel=1,
            )
            dataframe = dataframe.to_pandas()
        if ge_validate_kwargs is None:
            ge_validate_kwargs = {}
        report = great_expectations.from_pandas(
            dataframe, expectation_suite=expectation_suite
        ).validate(**ge_validate_kwargs)
        return report

    def set_job_group(self, group_id: str, description: Optional[str]) -> None:
        pass

    def convert_to_default_dataframe(
        self, dataframe: Union[pd.DataFrame, pl.DataFrame, pl.dataframe.frame.DataFrame]
    ) -> Optional[pd.DataFrame]:
        if (
            isinstance(dataframe, pd.DataFrame)
            or isinstance(dataframe, pl.DataFrame)
            or isinstance(dataframe, pl.dataframe.frame.DataFrame)
        ):
            upper_case_features = [
                col for col in dataframe.columns if any(re.finditer("[A-Z]", col))
            ]
            space_features = [col for col in dataframe.columns if " " in col]

            # make shallow copy so the original df does not get changed
            # this is always needed to keep the user df unchanged
            if isinstance(dataframe, pd.DataFrame):
                dataframe_copy = dataframe.copy(deep=False)
            else:
                dataframe_copy = dataframe.clone()

            # making a shallow copy of the dataframe so that column names are unchanged
            if len(upper_case_features) > 0:
                warnings.warn(
                    "The ingested dataframe contains upper case letters in feature names: `{}`. "
                    "Feature names are sanitized to lower case in the feature store.".format(
                        upper_case_features
                    ),
                    util.FeatureGroupWarning,
                    stacklevel=1,
                )
            if len(space_features) > 0:
                warnings.warn(
                    "The ingested dataframe contains feature names with spaces: `{}`. "
                    "Feature names are sanitized to use underscore '_' in the feature store.".format(
                        space_features
                    ),
                    util.FeatureGroupWarning,
                    stacklevel=1,
                )
            dataframe_copy.columns = [
                util.autofix_feature_name(x) for x in dataframe_copy.columns
            ]

            # convert timestamps with timezone to UTC
            for col in dataframe_copy.columns:
                if isinstance(
                    dataframe_copy[col].dtype, pd.core.dtypes.dtypes.DatetimeTZDtype
                ):
                    dataframe_copy[col] = dataframe_copy[col].dt.tz_convert(None)
                elif isinstance(dataframe_copy[col].dtype, pl.Datetime):
                    dataframe_copy = dataframe_copy.with_columns(
                        pl.col(col).dt.replace_time_zone(None)
                    )
            return dataframe_copy
        elif dataframe == "spine":
            return None

        raise TypeError(
            "The provided dataframe type is not recognized. Supported types are: pandas dataframe, polars dataframe. "
            + "The provided dataframe has type: {}".format(type(dataframe))
        )

    def parse_schema_feature_group(
        self,
        dataframe: Union[pd.DataFrame, pl.DataFrame],
        time_travel_format: Optional[str] = None,
        features: Optional[List[feature.Feature]] = None,
    ) -> List[feature.Feature]:
        feature_type_map = {}
        if features:
            for _feature in features:
                feature_type_map[_feature.name] = _feature.type
        if isinstance(dataframe, pd.DataFrame):
            arrow_schema = pa.Schema.from_pandas(dataframe, preserve_index=False)
        elif isinstance(dataframe, pl.DataFrame) or isinstance(
            dataframe, pl.dataframe.frame.DataFrame
        ):
            arrow_schema = dataframe.to_arrow().schema
        features = []
        for i in range(len(arrow_schema.names)):
            feat_name = arrow_schema.names[i]
            name = util.autofix_feature_name(feat_name)
            try:
                pd_type = arrow_schema.field(feat_name).type
                if pa.types.is_null(pd_type) and feature_type_map.get(name):
                    converted_type = feature_type_map.get(name)
                else:
                    converted_type = convert_pandas_dtype_to_offline_type(pd_type)
            except ValueError as e:
                raise FeatureStoreException(f"Feature '{name}': {str(e)}") from e
            features.append(feature.Feature(name, converted_type))

        return features

    def parse_schema_training_dataset(
        self, dataframe: Union[pd.DataFrame, pl.DataFrame]
    ) -> List[feature.Feature]:
        raise NotImplementedError(
            "Training dataset creation from Dataframes is not "
            + "supported in Python environment. Use HSFS Query object instead."
        )

    def save_dataframe(
        self,
        feature_group: FeatureGroup,
        dataframe: Union[pd.DataFrame, pl.DataFrame],
        operation: str,
        online_enabled: bool,
        storage: str,
        offline_write_options: Dict[str, Any],
        online_write_options: Dict[str, Any],
        validation_id: Optional[int] = None,
    ) -> Optional[job.Job]:
        # Currently on-demand transformation functions not supported in external feature groups.
        if (
            not isinstance(feature_group, ExternalFeatureGroup)
            and feature_group.transformation_functions
        ):
            dataframe = self._apply_transformation_function(
                feature_group.transformation_functions, dataframe
            )

        if (
            hasattr(feature_group, "EXTERNAL_FEATURE_GROUP")
            and feature_group.online_enabled
        ) or feature_group.stream:
            return self._write_dataframe_kafka(
                feature_group, dataframe, offline_write_options
            )
        else:
            # for backwards compatibility
            return self.legacy_save_dataframe(
                feature_group,
                dataframe,
                operation,
                online_enabled,
                storage,
                offline_write_options,
                online_write_options,
                validation_id,
            )

    def legacy_save_dataframe(
        self,
        feature_group: FeatureGroup,
        dataframe: Union[pd.DataFrame, pl.DataFrame],
        operation: str,
        online_enabled: bool,
        storage: str,
        offline_write_options: Dict[str, Any],
        online_write_options: Dict[str, Any],
        validation_id: Optional[int] = None,
    ) -> Optional[job.Job]:
        # App configuration
        app_options = self._get_app_options(offline_write_options)

        # Setup job for ingestion
        # Configure Hopsworks ingestion job
        print("Configuring ingestion job...")
        ingestion_job = self._feature_group_api.ingestion(feature_group, app_options)

        # Upload dataframe into Hopsworks
        print("Uploading Pandas dataframe...")
        self._dataset_api.upload_feature_group(
            feature_group, ingestion_job.data_path, dataframe
        )

        # run job
        ingestion_job.job.run(
            await_termination=offline_write_options is None
            or offline_write_options.get("wait_for_job", True)
        )

        return ingestion_job.job

    def get_training_data(
        self,
        training_dataset_obj: TrainingDataset,
        feature_view_obj: feature_view.FeatureView,
        query_obj: query.Query,
        read_options: Dict[str, Any],
        dataframe_type: str,
        training_dataset_version: int = None,
    ) -> Union[pd.DataFrame, pl.DataFrame]:
        """
        Function that creates or retrieves already created the training dataset.

        # Arguments
            training_dataset_obj `TrainingDataset`: The training dataset metadata object.
            feature_view_obj `FeatureView`: The feature view object for the which the training data is being created.
            query_obj `Query`: The query object that contains the query used to create the feature view.
            read_options `Dict[str, Any]`: Dictionary that can be used to specify extra parameters for reading data.
            dataframe_type `str`: The type of dataframe returned.
            training_dataset_version `int`: Version of training data to be retrieved.
        # Raises
            `ValueError`: If the training dataset statistics could not be retrieved.
        """

        # dataframe_type of list and numpy are prevented here because statistics needs to be computed from the returned dataframe.
        # The daframe is converted into required types in the function split_labels
        if dataframe_type.lower() not in ["default", "polars", "pandas"]:
            dataframe_type = "default"

        if training_dataset_obj.splits:
            return self._prepare_transform_split_df(
                query_obj,
                training_dataset_obj,
                feature_view_obj,
                read_options,
                dataframe_type,
                training_dataset_version,
            )
        else:
            df = query_obj.read(
                read_options=read_options, dataframe_type=dataframe_type
            )
            # if training_dataset_version is None:
            transformation_function_engine.TransformationFunctionEngine.compute_and_set_feature_statistics(
                training_dataset_obj, feature_view_obj, df
            )
            # else:
            #    transformation_function_engine.TransformationFunctionEngine.get_and_set_feature_statistics(
            #        training_dataset_obj, feature_view_obj, training_dataset_version
            #    )
            return self._apply_transformation_function(
                feature_view_obj.transformation_functions, df
            )

    def split_labels(
        self,
        df: Union[pd.DataFrame, pl.DataFrame],
        labels: List[str],
        dataframe_type: str,
    ) -> Tuple[
        Union[pd.DataFrame, pl.DataFrame], Optional[Union[pd.DataFrame, pl.DataFrame]]
    ]:
        if labels:
            labels_df = df[labels]
            df_new = df.drop(columns=labels)
            return (
                self._return_dataframe_type(df_new, dataframe_type),
                self._return_dataframe_type(labels_df, dataframe_type),
            )
        else:
            return self._return_dataframe_type(df, dataframe_type), None

    def drop_columns(
        self, df: Union[pd.DataFrame, pl.DataFrame], drop_cols: List[str]
    ) -> Union[pd.DataFrame, pl.DataFrame]:
        return df.drop(columns=drop_cols)

    def _prepare_transform_split_df(
        self,
        query_obj: query.Query,
        training_dataset_obj: TrainingDataset,
        feature_view_obj: feature_view.FeatureView,
        read_option: Dict[str, Any],
        dataframe_type: str,
        training_dataset_version: int = None,
    ) -> Dict[str, Union[pd.DataFrame, pl.DataFrame]]:
        """
        Split a df into slices defined by `splits`. `splits` is a `dict(str, int)` which keys are name of split
        and values are split ratios.

        # Arguments
            query_obj `Query`: The query object that contains the query used to create the feature view.
            training_dataset_obj `TrainingDataset`: The training dataset metadata object.
            feature_view_obj `FeatureView`: The feature view object for the which the training data is being created.
            read_options `Dict[str, Any]`: Dictionary that can be used to specify extra parameters for reading data.
            dataframe_type `str`: The type of dataframe returned.
            training_dataset_version `int`: Version of training data to be retrieved.
        # Raises
            `ValueError`: If the training dataset statistics could not be retrieved.
        """
        if (
            training_dataset_obj.splits[0].split_type
            == TrainingDatasetSplit.TIME_SERIES_SPLIT
        ):
            event_time = query_obj._left_feature_group.event_time
            if event_time not in [_feature.name for _feature in query_obj.features]:
                query_obj.append_feature(
                    query_obj._left_feature_group.__getattr__(event_time)
                )
                result_dfs = self._time_series_split(
                    query_obj.read(
                        read_options=read_option, dataframe_type=dataframe_type
                    ),
                    training_dataset_obj,
                    event_time,
                    drop_event_time=True,
                )
            else:
                result_dfs = self._time_series_split(
                    query_obj.read(
                        read_options=read_option, dataframe_type=dataframe_type
                    ),
                    training_dataset_obj,
                    event_time,
                )
        else:
            result_dfs = self._random_split(
                query_obj.read(read_options=read_option, dataframe_type=dataframe_type),
                training_dataset_obj,
            )

        # TODO : Currently statistics always computed since in memory training dataset retrieved is not consistent
        # if training_dataset_version is None:
        transformation_function_engine.TransformationFunctionEngine.compute_and_set_feature_statistics(
            training_dataset_obj, feature_view_obj, result_dfs
        )
        # else:
        #    transformation_function_engine.TransformationFunctionEngine.get_and_set_feature_statistics(
        #        training_dataset_obj, feature_view_obj, training_dataset_version
        #    )
        # and the apply them
        for split_name in result_dfs:
            result_dfs[split_name] = self._apply_transformation_function(
                feature_view_obj.transformation_functions,
                result_dfs.get(split_name),
            )

        return result_dfs

    def _random_split(
        self,
        df: Union[pd.DataFrame, pl.DataFrame],
        training_dataset_obj: TrainingDataset,
    ) -> Dict[str, Union[pd.DataFrame, pl.DataFrame]]:
        split_column = f"_SPLIT_INDEX_{uuid.uuid1()}"
        result_dfs = {}
        splits = training_dataset_obj.splits
        if (
            not math.isclose(
                sum([split.percentage for split in splits]), 1
            )  # relative tolerance = 1e-09
            or sum([split.percentage > 1 or split.percentage < 0 for split in splits])
            > 1
        ):
            raise ValueError(
                "Sum of split ratios should be 1 and each values should be in range (0, 1)"
            )

        df_size = len(df)
        groups = []
        for i, split in enumerate(splits):
            groups += [i] * int(df_size * split.percentage)
        groups += [len(splits) - 1] * (df_size - len(groups))
        random.shuffle(groups)
        if isinstance(df, pl.DataFrame) or isinstance(df, pl.dataframe.frame.DataFrame):
            df = df.with_columns(pl.Series(name=split_column, values=groups))
        else:
            df[split_column] = groups
        for i, split in enumerate(splits):
            if isinstance(df, pl.DataFrame) or isinstance(
                df, pl.dataframe.frame.DataFrame
            ):
                split_df = df.filter(pl.col(split_column) == i).drop(split_column)
            else:
                split_df = df[df[split_column] == i].drop(split_column, axis=1)
            result_dfs[split.name] = split_df
        return result_dfs

    def _time_series_split(
        self,
        df: Union[pd.DataFrame, pl.DataFrame],
        training_dataset_obj: TrainingDataset,
        event_time: str,
        drop_event_time: bool = False,
    ) -> Dict[str, Union[pd.DataFrame, pl.DataFrame]]:
        result_dfs = {}
        for split in training_dataset_obj.splits:
            if len(df[event_time]) > 0:
                result_df = df[
                    [
                        split.start_time
                        <= util.convert_event_time_to_timestamp(t)
                        < split.end_time
                        for t in df[event_time]
                    ]
                ]
            else:
                # if df[event_time] is empty, it returns an empty dataframe
                result_df = df
            if drop_event_time:
                result_df = result_df.drop([event_time], axis=1)
            result_dfs[split.name] = result_df
        return result_dfs

    def write_training_dataset(
        self,
        training_dataset: TrainingDataset,
        dataset: Union[query.Query, pd.DataFrame, pl.DataFrame],
        user_write_options: Dict[str, Any],
        save_mode: str,
        feature_view_obj: Optional[feature_view.FeatureView] = None,
        to_df: bool = False,
    ) -> Union["job.Job", Any]:
        if not feature_view_obj and not isinstance(dataset, query.Query):
            raise Exception(
                "Currently only query based training datasets are supported by the Python engine"
            )

        if (
            arrow_flight_client.is_query_supported(dataset, user_write_options)
            and len(training_dataset.splits) == 0
            and feature_view_obj
            and len(feature_view_obj.transformation_functions) == 0
            and training_dataset.data_format == "parquet"
        ):
            query_obj, _ = dataset._prep_read(False, user_write_options)
            response = util.run_with_loading_animation(
                "Materializing data to Hopsworks, using Hopsworks Feature Query Service",
                arrow_flight_client.get_instance().create_training_dataset,
                feature_view_obj,
                training_dataset,
                query_obj,
                user_write_options.get("arrow_flight_config", {}),
            )

            return response

        # As for creating a feature group, users have the possibility of passing
        # a spark_job_configuration object as part of the user_write_options with the key "spark"
        spark_job_configuration = user_write_options.pop("spark", None)
        td_app_conf = training_dataset_job_conf.TrainingDatasetJobConf(
            query=dataset,
            overwrite=(save_mode == "overwrite"),
            write_options=user_write_options,
            spark_job_configuration=spark_job_configuration,
        )

        if feature_view_obj:
            fv_api = feature_view_api.FeatureViewApi(feature_view_obj.featurestore_id)
            td_job = fv_api.compute_training_dataset(
                feature_view_obj.name,
                feature_view_obj.version,
                training_dataset.version,
                td_app_conf,
            )
        else:
            td_api = training_dataset_api.TrainingDatasetApi(
                training_dataset.feature_store_id
            )
            td_job = td_api.compute(training_dataset, td_app_conf)
        print(
            "Training dataset job started successfully, you can follow the progress at \n{}".format(
                util.get_job_url(td_job.href)
            )
        )

        td_job._wait_for_job(
            await_termination=user_write_options.get("wait_for_job", True)
        )
        return td_job

    def _return_dataframe_type(
        self, dataframe: Union[pd.DataFrame, pl.DataFrame], dataframe_type: str
    ) -> Union[pd.DataFrame, pl.DataFrame, np.ndarray, List[List[Any]]]:
        """
        Returns a dataframe of particular type.

        # Arguments
            dataframe `Union[pd.DataFrame, pl.DataFrame]`: Input dataframe
            dataframe_type `str`: Type of dataframe to be returned
        # Returns
            `Union[pd.DataFrame, pl.DataFrame, np.array, list]`: DataFrame of required type.
        """
        if dataframe_type.lower() in ["default", "pandas"]:
            return dataframe
        if dataframe_type.lower() == "polars":
            if not (
                isinstance(dataframe, pl.DataFrame) or isinstance(dataframe, pl.Series)
            ):
                return pl.from_pandas(dataframe)
            else:
                return dataframe
        if dataframe_type.lower() == "numpy":
            return dataframe.values
        if dataframe_type.lower() == "python":
            return dataframe.values.tolist()

        raise TypeError(
            "Dataframe type `{}` not supported on this platform.".format(dataframe_type)
        )

    def is_spark_dataframe(
        self, dataframe: Union[pd.DataFrame, pl.DataFrame]
    ) -> Literal[False]:
        return False

    def save_stream_dataframe(
        self,
        feature_group: Union[FeatureGroup, ExternalFeatureGroup],
        dataframe: Union[pd.DataFrame, pl.DataFrame],
        query_name: Optional[str],
        output_mode: Optional[str],
        await_termination: bool,
        timeout: Optional[int],
        write_options: Optional[Dict[str, Any]],
    ) -> None:
        raise NotImplementedError(
            "Stream ingestion is not available on Python environments, because it requires Spark as engine."
        )

    def save_empty_dataframe(
        self, feature_group: Union[FeatureGroup, ExternalFeatureGroup]
    ) -> None:
        """Wrapper around save_dataframe in order to provide no-op."""
        pass

    def _get_app_options(
        self, user_write_options: Optional[Dict[str, Any]] = None
    ) -> ingestion_job_conf.IngestionJobConf:
        """
        Generate the options that should be passed to the application doing the ingestion.
        Options should be data format, data options to read the input dataframe and
        insert options to be passed to the insert method

        Users can pass Spark configurations to the save/insert method
        Property name should match the value in the JobConfiguration.__init__
        """
        spark_job_configuration = (
            user_write_options.pop("spark", None) if user_write_options else None
        )

        return ingestion_job_conf.IngestionJobConf(
            data_format="PARQUET",
            data_options=[],
            write_options=user_write_options or {},
            spark_job_configuration=spark_job_configuration,
        )

    def add_file(self, file: Optional[str]) -> Optional[str]:
        if not file:
            return file

        # This is used for unit testing
        if not file.startswith("file://"):
            file = "hdfs://" + file

        local_file = os.path.join("/tmp", os.path.basename(file))
        if not os.path.exists(local_file):
            content_stream = self._dataset_api.read_content(
                file, util.get_dataset_type(file)
            )
            bytesio_object = BytesIO(content_stream.content)
            # Write the stuff
            with open(local_file, "wb") as f:
                f.write(bytesio_object.getbuffer())
        return local_file

    def _apply_transformation_function(
        self,
        transformation_functions: List[transformation_function.TransformationFunction],
        dataset: Union[pd.DataFrame, pl.DataFrame],
    ) -> Union[pd.DataFrame, pl.DataFrame]:
        """
        Apply transformation function to the dataframe.

        # Arguments
            transformation_functions `List[transformation_function.TransformationFunction]` : List of transformation functions.
            dataset `Union[pd.DataFrame, pl.DataFrame]`: A pandas or polars dataframe.
        # Returns
            `DataFrame`: A pandas dataframe with the transformed data.
        # Raises
            `FeatureStoreException`: If any of the features mentioned in the transformation function is not present in the Feature View.
        """
        dropped_features = set()

        if isinstance(dataset, pl.DataFrame) or isinstance(
            dataset, pl.dataframe.frame.DataFrame
        ):
            # Converting polars dataframe to pandas because currently we support only pandas UDF's as transformation functions.
            if HAS_ARROW:
                dataset = dataset.to_pandas(
                    use_pyarrow_extension_array=True
                )  # Zero copy if pyarrow extension can be used.
            else:
                dataset = dataset.to_pandas(use_pyarrow_extension_array=False)

        for tf in transformation_functions:
            hopsworks_udf = tf.hopsworks_udf
            missing_features = set(hopsworks_udf.transformation_features) - set(
                dataset.columns
            )
            if missing_features:
                raise FeatureStoreException(
                    f"Features {missing_features} specified in the transformation function '{hopsworks_udf.function_name}' are not present in the feature view. Please specify the feature required correctly."
                )
            if tf.hopsworks_udf.dropped_features:
                dropped_features.update(tf.hopsworks_udf.dropped_features)
            dataset = pd.concat(
                [
                    dataset.reset_index(drop=True),
                    tf.hopsworks_udf.get_udf()(
                        *(
                            [
                                dataset[feature]
                                for feature in tf.hopsworks_udf.transformation_features
                            ]
                        )
                    ).reset_index(drop=True),
                ],
                axis=1,
            )
        dataset = dataset.drop(dropped_features, axis=1)
        return dataset

    @staticmethod
    def get_unique_values(
        feature_dataframe: Union[pd.DataFrame, pl.DataFrame], feature_name: str
    ) -> np.ndarray:
        return feature_dataframe[feature_name].unique()

    def _write_dataframe_kafka(
        self,
        feature_group: Union[FeatureGroup, ExternalFeatureGroup],
        dataframe: Union[pd.DataFrame, pl.DataFrame],
        offline_write_options: Dict[str, Any],
    ) -> Optional[job.Job]:
        initial_check_point = ""
        producer, headers, feature_writers, writer = kafka_engine.init_kafka_resources(
            feature_group,
            offline_write_options,
            project_id=client.get_instance()._project_id,
        )
        if not feature_group._multi_part_insert:
            # set initial_check_point to the current offset
            initial_check_point = kafka_engine.kafka_get_offsets(
                topic_name=feature_group._online_topic_name,
                feature_store_id=feature_group.feature_store_id,
                offline_write_options=offline_write_options,
                high=True,
            )

        acked, progress_bar = kafka_engine.build_ack_callback_and_optional_progress_bar(
            n_rows=dataframe.shape[0],
            is_multi_part_insert=feature_group._multi_part_insert,
            offline_write_options=offline_write_options,
        )

        if isinstance(dataframe, pd.DataFrame):
            row_iterator = dataframe.itertuples(index=False)
        else:
            row_iterator = dataframe.iter_rows(named=True)

        # loop over rows
        for row in row_iterator:
            if isinstance(dataframe, pd.DataFrame):
                # itertuples returns Python NamedTyple, to be able to serialize it using
                # avro, create copy of row only by converting to dict, which preserves datatypes
                row = row._asdict()

                # transform special data types
                # here we might need to handle also timestamps and other complex types
                # possible optimizaiton: make it based on type so we don't need to loop over
                # all keys in the row
                for k in row.keys():
                    # for avro to be able to serialize them, they need to be python data types
                    if isinstance(row[k], np.ndarray):
                        row[k] = row[k].tolist()
                    if isinstance(row[k], pd.Timestamp):
                        row[k] = row[k].to_pydatetime()
                    if isinstance(row[k], datetime) and row[k].tzinfo is None:
                        row[k] = row[k].replace(tzinfo=timezone.utc)
                    if isinstance(row[k], pd._libs.missing.NAType):
                        row[k] = None

            # encode complex features
            row = kafka_engine.encode_complex_features(feature_writers, row)

            # encode feature row
            with BytesIO() as outf:
                writer(row, outf)
                encoded_row = outf.getvalue()

            # assemble key
            key = "".join([str(row[pk]) for pk in sorted(feature_group.primary_key)])

            kafka_engine.kafka_produce(
                producer=producer,
                key=key,
                encoded_row=encoded_row,
                topic_name=feature_group._online_topic_name,
                headers=headers,
                acked=acked,
                debug_kafka=offline_write_options.get("debug_kafka", False),
            )

        # make sure producer blocks and everything is delivered
        if not feature_group._multi_part_insert:
            producer.flush()
            progress_bar.close()

        # start materialization job if not an external feature group, otherwise return None
        if isinstance(feature_group, ExternalFeatureGroup):
            return None
        # if topic didn't exist, always run the materialization job to reset the offsets except if it's a multi insert
        if not initial_check_point and not feature_group._multi_part_insert:
            if self._start_offline_materialization(offline_write_options):
                warnings.warn(
                    "This is the first ingestion after an upgrade or backup/restore, running materialization job even though `start_offline_materialization` was set to `False`.",
                    util.FeatureGroupWarning,
                    stacklevel=1,
                )
            # set the initial_check_point to the lowest offset (it was not set previously due to topic not existing)
            initial_check_point = kafka_engine.kafka_get_offsets(
                topic_name=feature_group._online_topic_name,
                feature_store_id=feature_group.feature_store_id,
                offline_write_options=offline_write_options,
                high=True,
            )
            now = datetime.now(timezone.utc)
            feature_group.materialization_job.run(
                args=feature_group.materialization_job.config.get("defaultArgs", "")
                + initial_check_point,
                await_termination=offline_write_options.get("wait_for_job", False),
            )
            offline_backfill_every = offline_write_options.pop(
                "offline_backfill_every", None
            )
            if offline_backfill_every:
                if isinstance(offline_backfill_every, str):
                    cron_expression = offline_backfill_every
                elif isinstance(offline_backfill_every, int):
                    cron_expression = f"{now.second} {now.minute} {now.hour}/{offline_backfill_every} ? * * *"
                feature_group.materialization_job.schedule(
                    cron_expression=cron_expression,
                    # added 2 seconds after the current time to avoid retriggering the job directly
                    start_time=now + timedelta(seconds=2),
                )
            else:
                _logger.info("Materialisation job was not scheduled.")

        elif self._start_offline_materialization(offline_write_options):
            if not offline_write_options.get(
                "skip_offsets", False
            ) and self._job_api.last_execution(
                feature_group.materialization_job
            ):  # always skip offsets if executing job for the first time
                # don't provide the current offsets (read from where the job last left off)
                initial_check_point = ""
            # provide the initial_check_point as it will reduce the read amplification of materialization job
            feature_group.materialization_job.run(
                args=feature_group.materialization_job.config.get("defaultArgs", "")
                + initial_check_point,
                await_termination=offline_write_options.get("wait_for_job", False),
            )
        return feature_group.materialization_job

    @staticmethod
    def cast_columns(
        df: pd.DataFrame, schema: List[feature.Feature], online: bool = False
    ) -> pd.DataFrame:
        for _feat in schema:
            if not online:
                df[_feat.name] = cast_column_to_offline_type(df[_feat.name], _feat.type)
            else:
                df[_feat.name] = cast_column_to_online_type(
                    df[_feat.name], _feat.online_type
                )
        return df

    @staticmethod
    def is_connector_type_supported(connector_type: str) -> bool:
        return connector_type in [
            sc.StorageConnector.HOPSFS,
            sc.StorageConnector.S3,
            sc.StorageConnector.KAFKA,
        ]

    @staticmethod
    def _start_offline_materialization(offline_write_options: Dict[str, Any]) -> bool:
        if offline_write_options is not None:
            if "start_offline_materialization" in offline_write_options:
                return offline_write_options.get("start_offline_materialization")
            elif "start_offline_backfill" in offline_write_options:
                return offline_write_options.get("start_offline_backfill")
            else:
                return True
        else:
            return True

    @staticmethod
    def _convert_feature_log_to_df(feature_log, cols) -> pd.DataFrame:
        if feature_log is None and cols:
            return pd.DataFrame(columns=cols)
        if not (
            isinstance(feature_log, (list, np.ndarray, pd.DataFrame, pl.DataFrame))
        ):
            raise ValueError(f"Type '{type(feature_log)}' not accepted")
        if isinstance(feature_log, list) or isinstance(feature_log, np.ndarray):
            if isinstance(feature_log[0], list) or isinstance(
                feature_log[0], np.ndarray
            ):
                provided_len = len(feature_log[0])
            else:
                provided_len = 1
            assert provided_len == len(
                cols
            ), f"Expecting {len(cols)} features/labels but {provided_len} provided."

            return pd.DataFrame(feature_log, columns=cols)
        else:
            if isinstance(feature_log, pl.DataFrame):
                return feature_log.clone().to_pandas()
            elif isinstance(feature_log, pd.DataFrame):
                return feature_log.copy(deep=False)

    @staticmethod
    def get_feature_logging_df(
        features: Union[pd.DataFrame, list[list], np.ndarray],
        fg: FeatureGroup = None,
        td_features: List[str] = None,
        td_predictions: List[TrainingDatasetFeature] = None,
        td_col_name: Optional[str] = None,
        time_col_name: Optional[str] = None,
        model_col_name: Optional[str] = None,
        predictions: Optional[Union[pd.DataFrame, list[list], np.ndarray]] = None,
        training_dataset_version: Optional[int] = None,
        hsml_model=None,
    ) -> pd.DataFrame:
        features = Engine._convert_feature_log_to_df(features, td_features)
        if td_predictions:
            predictions = Engine._convert_feature_log_to_df(
                predictions, [f.name for f in td_predictions]
            )
            for f in td_predictions:
                predictions[f.name] = cast_column_to_offline_type(
                    predictions[f.name], f.type
                )
            if not set(predictions.columns).intersection(set(features.columns)):
                features = pd.concat([features, predictions], axis=1)

        features[td_col_name] = pd.Series(
            [training_dataset_version for _ in range(len(features))]
        )
        # _cast_column_to_offline_type cannot cast string type
        features[model_col_name] = pd.Series(
            [
                FeatureViewEngine.get_hsml_model_value(hsml_model)
                if hsml_model
                else None
                for _ in range(len(features))
            ],
            dtype=pd.StringDtype(),
        )
        now = datetime.now()

        features[time_col_name] = pd.Series([now for _ in range(len(features))])
        features["log_id"] = [str(uuid.uuid4()) for _ in range(len(features))]
        return features[[feat.name for feat in fg.features]]

    @staticmethod
    def read_feature_log(query):
        df = query.read()
        return df.drop(["log_id", FeatureViewEngine._LOG_TIME], axis=1)
