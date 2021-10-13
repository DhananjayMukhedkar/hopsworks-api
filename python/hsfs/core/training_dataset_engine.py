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

import re
import io

import avro.schema
import avro.io
from sqlalchemy import sql, bindparam

from hsfs import engine, training_dataset_feature, util
from hsfs.core import (
    training_dataset_api,
    tags_api,
    storage_connector_api,
    transformation_function_engine,
)
from hsfs.constructor import query


class TrainingDatasetEngine:
    OVERWRITE = "overwrite"
    APPEND = "append"
    ENTITY_TYPE = "trainingdatasets"

    def __init__(self, feature_store_id):
        self._training_dataset_api = training_dataset_api.TrainingDatasetApi(
            feature_store_id
        )
        self._tags_api = tags_api.TagsApi(feature_store_id, self.ENTITY_TYPE)
        self._storage_connector_api = storage_connector_api.StorageConnectorApi(
            feature_store_id
        )
        self._transformation_function_engine = (
            transformation_function_engine.TransformationFunctionEngine(
                feature_store_id
            )
        )

    def save(self, training_dataset, features, user_write_options):
        if isinstance(features, query.Query):
            training_dataset._querydto = features
            training_dataset._features = [
                training_dataset_feature.TrainingDatasetFeature(
                    name=label_name, label=True
                )
                for label_name in training_dataset.label
            ]
            self._transformation_function_engine.attach_transformation_fn(
                training_dataset
            )
        else:
            features = engine.get_instance().convert_to_default_dataframe(features)
            training_dataset._features = (
                engine.get_instance().parse_schema_training_dataset(features)
            )
            for label_name in training_dataset.label:
                for feature in training_dataset._features:
                    if feature.name == label_name:
                        feature.label = True

            # check if user provided transformation functions and throw error as transformation functions work only
            # with query objects
            if training_dataset.transformation_functions:
                raise ValueError(
                    "Transformation functions can only be applied to training datasets generated from Query object"
                )

        updated_instance = self._training_dataset_api.post(training_dataset)
        td_job = engine.get_instance().write_training_dataset(
            training_dataset, features, user_write_options, self.OVERWRITE
        )
        return updated_instance, td_job

    def insert(self, training_dataset, dataset, user_write_options, overwrite):
        return engine.get_instance().write_training_dataset(
            training_dataset,
            dataset,
            user_write_options,
            self.OVERWRITE if overwrite else self.APPEND,
        )

    def read(self, training_dataset, split, user_read_options):
        read_options = engine.get_instance().read_options(
            training_dataset.data_format, user_read_options
        )

        if split is not None:
            path = training_dataset.location + "/" + str(split)
        else:
            path = training_dataset.location

        return training_dataset.storage_connector.read(
            # always read from materialized dataset, not query object
            query=None,
            data_format=training_dataset.data_format,
            options=read_options,
            path=path,
        )

    def query(self, training_dataset, online, with_label):
        fs_query = self._training_dataset_api.get_query(training_dataset, with_label)

        if online:
            return fs_query["queryOnline"]
        if fs_query["pitQuery"] is not None:
            return fs_query["pitQuery"]
        return fs_query["query"]

    def add_tag(self, training_dataset, name, value):
        """Attach a name/value tag to a training dataset."""
        self._tags_api.add(training_dataset, name, value)

    def delete_tag(self, training_dataset, name):
        """Remove a tag from a training dataset."""
        self._tags_api.delete(training_dataset, name)

    def get_tag(self, training_dataset, name):
        """Get tag with a certain name for a training dataset."""
        return self._tags_api.get(training_dataset, name)[name]

    def get_tags(self, training_dataset):
        """Get all tags for a training dataset."""
        return self._tags_api.get(training_dataset)

    def update_statistics_config(self, training_dataset):
        """Update the statistics configuration of a feature group."""
        self._training_dataset_api.update_metadata(
            training_dataset, training_dataset, "updateStatsConfig"
        )

    def get_complex_feature_schemas(self, training_dataset):
        return {
            f.name: avro.io.DatumReader(
                avro.schema.parse(f._feature_group._get_feature_avro_schema(f.name))
            )
            for f in training_dataset.schema
            if f.is_complex()
        }

    def deserialize_complex_features(self, feature_schemas, row_dict):
        for feature_name, schema in feature_schemas.items():
            bytes_reader = io.BytesIO(row_dict[feature_name])
            decoder = avro.io.BinaryDecoder(bytes_reader)
            row_dict[feature_name] = schema.read(decoder)
        return row_dict

    def get_serving_vector(self, training_dataset, entry, external):
        """Assembles serving vector from online feature store."""

        # initialize prepared statements
        if training_dataset.prepared_statements is None:
            if hasattr(list(entry.values())[0], "__iter__") and isinstance(
                list(list(entry.values())[0]), list
            ):
                self.init_prepared_statement(
                    training_dataset, len(list(entry.values())[0]), external
                )
            else:
                self.init_prepared_statement(training_dataset, None, external)

        # if serving_batch_size is set then its batch lookup.
        if (
            training_dataset.serving_batch_size is not None
            and training_dataset.serving_batch_size > 1
        ):
            if not hasattr(list(entry.values())[0], "__iter__") and not isinstance(
                list(entry.values())[0], list
            ):
                raise ValueError(
                    "Entry is expected to be list of primary key values. "
                    "If you have already initialised for batch serving and now want to retrieve single vector "
                    "please reinitialise prepared statements with  `training_dataset.init_prepared_statement()`"
                )
            # check if size of batch of keys corresponds to one that was used during initialisation.
            batch_sizes = list(set([len(x) for x in entry.values()]))
            if (
                len(batch_sizes) != 1
                and batch_sizes != training_dataset.serving_batch_size
            ):
                raise ValueError(
                    "Size of provided batch of primary keys doesn't correspond to "
                    + "size used during initialisation"
                )
            # create dict object that will have of order of the vector as key and values as
            # vector itself to stitch them correctly if there are multiple feature groups involved. At this point we
            # expect that backend will return correctly ordered vectors.
            batch_dicts = {}
            batch = True
        else:
            if hasattr(list(entry.values())[0], "__iter__") and isinstance(
                list(entry.values())[0], list
            ):
                raise ValueError(
                    "Entry is expected to be single value per primary key. "
                    "If you have already initialised prepared statements for single vector and now want to retrieve "
                    "batch vector please reinitialise prepared statements with  "
                    "`training_dataset.init_prepared_statement(batch_size=n)`"
                )
            batch = False
            serving_vector = []

        # check if primary key map correspond to serving_keys.
        if not entry.keys() == training_dataset.serving_keys:
            raise ValueError(
                "Provided primary key map doesn't correspond to serving_keys"
            )

        prepared_statements = training_dataset.prepared_statements

        # get schemas for complex features once
        complex_features = self.get_complex_feature_schemas(training_dataset)

        for prepared_statement_index in training_dataset.prepared_statements:
            order_in_batch = 0
            prepared_statement = prepared_statements[prepared_statement_index]
            with training_dataset.prepared_statement_engine.connect() as mysql_conn:
                result_proxy = mysql_conn.execute(prepared_statement, entry).fetchall()
            result_dict = {}
            for row in result_proxy:
                result_dict = self.deserialize_complex_features(
                    complex_features, dict(row.items())
                )
                if not result_dict:
                    raise Exception(
                        "No data was retrieved from online feature store using input "
                        + entry
                    )
                # apply transformation functions
                result_dict = self._apply_transformation(
                    training_dataset.transformation_functions, result_dict
                )

                # if this is batch serving then get vector by order and update with vector from other feature group(s)
                if batch:
                    if order_in_batch in batch_dicts:
                        batch_dicts[order_in_batch] += list(result_dict.values())
                    else:
                        batch_dicts[order_in_batch] = list(result_dict.values())
                order_in_batch += 1

            if not batch:
                serving_vector += list(result_dict.values())

        # if this is batch serving then return batch of vectors, otherwise return single serving vector
        if batch:
            return list(batch_dicts.values())
        else:
            return serving_vector

    def init_prepared_statement(self, training_dataset, batch_size, external):

        # reset values to default, as user may be re-initialising with different parameters
        training_dataset.prepared_statement_engine = None
        training_dataset.prepared_statements = None
        training_dataset.serving_keys = None
        training_dataset.serving_batch_size = None

        if batch_size is not None and batch_size > 1:
            batch = True
            training_dataset.serving_batch_size = batch_size
        else:
            batch = False
        online_conn = self._storage_connector_api.get_online_connector()
        mysql_engine = util.create_mysql_engine(online_conn, external)
        prepared_statements = self._training_dataset_api.get_serving_prepared_statement(
            training_dataset, batch
        )

        prepared_statements_dict = {}
        serving_vector_keys = set()
        for prepared_statement in prepared_statements:
            query_online = str(prepared_statement.query_online).replace("\n", " ")

            # In java prepared statement `?` is used for parametrization.
            # In sqlalchemy `:feature_name` is used instead of `?`
            pk_names = [
                psp.name
                for psp in sorted(
                    prepared_statement.prepared_statement_parameters,
                    key=lambda psp: psp.index,
                )
            ]
            # Now we have ordered pk_names, iterate over it and replace `?` with `:feature_name` one by one.
            # Regex `"^(.*?)\?"` will identify 1st occurrence of `?` in the sql string and replace it with provided
            # feature name, e.g. `:feature_name`:. As we iteratively update `query_online` we are always aiming to
            # replace 1st occurrence of `?`. This approach can only work if primary key names are sorted properly.
            # Regex `"^(.*?)\?"`:
            # `^` - asserts position at start of a line
            # `.*?` - matches any character (except for line terminators). `*?` Quantifier —  Matches between zero and
            # unlimited times, expanding until needed, i.e 1st occurrence of `\?` character.
            for pk_name in pk_names:
                serving_vector_keys.add(pk_name)
                query_online = re.sub(
                    r"^(.*?)\?",
                    r"\1:" + pk_name,
                    query_online,
                )
            query_online = sql.text(query_online)
            # in case of batch serving vector bind parameters to the prepared statement.
            if batch:
                bind_params = [
                    bindparam(pk_name, expanding=True) for pk_name in pk_names
                ]
                query_online = query_online.bindparams(*bind_params)
            prepared_statements_dict[
                prepared_statement.prepared_statement_index
            ] = query_online

        # attach transformation functions
        training_dataset.transformation_functions = (
            self._transformation_function_engine.get_td_transformation_fn(
                training_dataset
            )
        )

        training_dataset.prepared_statement_engine = mysql_engine
        training_dataset.prepared_statements = prepared_statements_dict
        training_dataset.serving_keys = serving_vector_keys

    @staticmethod
    def _apply_transformation(transformation_fns, row_dict):
        for feature_name in transformation_fns:
            if feature_name in row_dict:
                transformation_fn = transformation_fns[feature_name].transformation_fn
                row_dict[feature_name] = transformation_fn(row_dict[feature_name])
        return row_dict
