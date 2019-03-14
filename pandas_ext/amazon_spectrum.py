"""Tools for creating Amazon Spectrum schemas."""
from inspect import cleandoc
from os import getenv

import pandas as pd
import requests

from pandas_ext.common.utils import today
from pandas_ext.parquet import to_parquet
from pandas_ext.sqla_utils import schema_from_df, schema_from_registry


def _get_file_format_serde(file_format: str) -> str:
    return dict(
        parquet='org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe'
    )[file_format]


def _build_s3_stream_path(
    bucket,
    stream,
    file_format,
    partition,
    partition_value,
    has_partition
):
    partitioned_by = (
        f'{partition}={partition_value}'
        if has_partition
        else ''
    )
    return cleandoc(f"""
        s3://{bucket}/{stream}/ext={file_format}/{partitioned_by}{stream}
        /{stream}.snappy"""
                    ).lower()


def _create_schema_alias_statement(
        schema_alias: str,
        schema: str,
        table: str
) -> str:
    return cleandoc(f"""
        CREATE VIEW "{schema_alias}"."{table}" AS
        SELECT * FROM "{schema}"."{table}"
        WITH NO SCHEMA BINDING;
    """
                    )


def _create_partition_statement(
        schema: str,
        bucket: str,
        stream: str,
        file_format: str= 'parquet',
        partition: str= 'dt',
        partition_value: str= ''
) -> str:
    partition_value = (
        partition_value if partition_value else
        today()
    )
    s3_path = (
        f's3://{bucket}/{stream}/ext={file_format}/'
        f'{partition}={partition_value}/'
    ).lower()
    return cleandoc(f"""
        ALTER TABLE "{schema}"."{stream}_{file_format}"
        ADD PARTITION ({partition}='{partition_value}')
        LOCATION '{s3_path}'
        ;"""
                    )


def _external_table_exists_statement(
        schema: str,
        table: str
) -> str:

    return cleandoc(f"""
        SELECT distinct(schemaname || tablename) as schema_table
        FROM SVV_EXTERNAL_COLUMNS
        WHERE schemaname || tablename = '{schema}{table}'
        ;"""
                    )


def _create_external_table_statement(
        schema: str,
        table: str,
        columns: str,
        bucket: str,
        stream: str,
        file_format: str = 'parquet',
        partition: str = 'dt',
        partition_type: str = 'date',
        partition_value: str = '',
        has_partition: bool = True
) -> str:
    serde = _get_file_format_serde(file_format)
    upper_file_format = file_format.upper()
    s3_path = f's3://{bucket}/{stream}/ext={file_format}/'.lower()
    partitioned_by = (
        f'PARTITIONED BY ({partition} {partition_type})'
        if has_partition
        else ''
    )

    return cleandoc(f"""
        CREATE EXTERNAL TABLE "{schema}"."{table}_{file_format}" (
        {columns}
        )
        {partitioned_by}
        ROW FORMAT SERDE '{serde}'
        STORED AS {upper_file_format}
        LOCATION '{s3_path}'
        ;"""
                    )


def to_spectrum(
        table: str,
        external_schema: str,
        bucket: str,
        df: pd.DataFrame = pd.DataFrame(),
        schema_alias: str = '',
        stream: str = '',
        file_format: str = 'parquet',
        partition: str = 'dt',
        partition_type: str = 'date',
        partition_value: str = '',
        conn: str = '',
        has_partition: bool = True,
        **kwargs
) -> str:
    """Sends your dataframe to Spectrum for use in Athena/Redshift/Looker/etc

       Currently we only print out the statements as only the owner of the
       external schema can actually run the CREATE EXTERNAL TABLE statement.

       table: table name as it appears in Spectrum
       external_schema: external table schema
       bucket: s3 bucket
       df: pandas Dataframe
       schema_alias: If you want to create an alternate path to your schema
       stream: Defaults to table if not provided.
       file_format: Defaults to parquet and may expand to avro.
       partition: Defaults to dt, which is short for the date.
       partition_type: The data type declaration of the partition value.
       partition_value: Defaults to todays date.
       conn: A valid sqlalchemy string to connect to spectrum.
       kwargs: kwargs you want to pass to `to_parquet()` call
       has_partition: Remove partition altogether from output
    """

    stream = stream if stream else table
    columns = (
            schema_from_df(df)
            if len(df) != 0 else
            schema_from_registry(stream)
        )
    external_table_statement = _create_external_table_statement(
        schema=external_schema,
        table=table,
        columns=columns,
        bucket=bucket,
        stream=stream,
        file_format=file_format,
        partition=partition,
        partition_type=partition_type,
        has_partition=has_partition
    )
    alias_statement = (
        '' if not schema_alias else
        _create_schema_alias_statement(schema_alias, external_schema, table)
    )
    if has_partition:
        partition_value = (
            partition_value if partition_value else
            today()
        )
        partition_statement = _create_partition_statement(
            schema=external_schema,
            bucket=bucket,
            stream=stream,
            partition=partition,
            partition_value=partition_value
        )
    else:
        partition_statement = ''
    create_statement = (''.join([
        external_table_statement,
        alias_statement,
        partition_statement
    ]))

    if conn:
        from sqlalchemy import create_engine
        engine = create_engine(conn, execution_options=dict(autocommit=True))

        schema_table_statement = _external_table_exists_statement(
            external_schema, table)
        table_exists_data = pd.read_sql_query(schema_table_statement, conn)
        if len(table_exists_data) == 0:
            # table doesn't exist so create it.
            engine.execute(create_statement)
        engine.execute(partition_statement)
    return create_statement
