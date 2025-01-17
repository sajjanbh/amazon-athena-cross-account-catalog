# Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import time
import base64, json, os

from heracles.hive.hive_metastore import ttypes


class HiveMappers:
    @staticmethod
    def map_glue_database(glue_database):
        return ttypes.Database(
            name=glue_database.get('Name'),
            description=glue_database.get('Description', ""),
            locationUri=glue_database.get('LocationUri', ""),
            parameters=glue_database.get('Parameters', {})
        )

    @staticmethod
    def map_presto_view(view_name, view_text, catalog):
        b64_text = view_text.split(" ")[3]   # fetch base64 encoded string
        b_encode_text = b64_text.encode()
        plain_text = base64.b64decode(b_encode_text)
        
        try:
            view_json = json.loads(plain_text)
        except Exception as e:
            print("{} doesn't contain a valid Presto View definition.".format(view_name))
            return view_text, None
        
        view_json['catalog'] = catalog
        
        plain_text = json.dumps(view_json)
        b_encode_text = base64.b64encode(plain_text.encode())
        return "/* Presto View: {} */".format(b_encode_text.decode()), view_json['originalSql']
        
    # ---------- To handle Hive View (experimental) -------
    @staticmethod
    def map_hive_view(view_text, catalog, db, columns):
        # Try to make Presto compatible JSON and base64 encode
        view_json = {}
        view_json['originalSql'] = view_text
        view_json['catalog']= catalog
        view_json['schema'] = db
        new_cols = []
        for column in columns:
            new_col = {}
            for k, v in column.items():
                # There are some syntatically differences between Hive and Presto View schema. Attempt to make them compatible with Presto.
                v = v.replace("string", "varchar")
                v = v.replace("struct", "row")
                v = v.replace(":", " ")
                v = v.replace("<", "(")
                v = v.replace(">", ")")
                new_col[k.lower()] = v
            new_cols.append(new_col)
        view_json['columns'] = new_cols
        b_encode_text = base64.b64encode(json.dumps(view_json).encode())
        return "/* Presto View: {} */".format(b_encode_text.decode())
    
    @staticmethod
    def map_glue_table(databaseName, tableName, glue_table):
        # Create the base table type
        table = ttypes.Table(
            tableName=tableName,
            dbName=databaseName,
            owner=glue_table.get('Owner', None),
            createTime=0,
            lastAccessTime=HiveMappers.unix_epoch_as_int(glue_table.get('LastAccessTime', None)),
            retention=glue_table.get('Retention', None),
            tableType=glue_table.get('TableType'),
            parameters=glue_table.get('Parameters', {}),
            viewOriginalText=None,
            viewExpandedText=None,
            partitionKeys=[
                ttypes.FieldSchema(
                    name=key['Name'],
                    type=key['Type']
                ) for key in glue_table.get('PartitionKeys', [])
            ]
        )
        
        # To distinguish View from External table
        if glue_table['TableType'] == "VIRTUAL_VIEW":
            # Console isn't listing view correctly, so setting it to some other value to make them list under Tables section.
            table.tableType = "PRESTO_VIEW"
            # Manipulating the catalog within ViewOriginalText so that it doesn't point to original catalog name
            # Set the catalog name to the one being defined in Athena. This is derived from ENV variable. If not set, it'll have default catalog name "AwsDataCatalog"
            if 'CATALOG_NAME' in os.environ:
                catalog = os.environ['CATALOG_NAME']
            else:
                catalog=''
                print("Env variable 'CATALOG_NAME' not set to the corresponding catalog/data source name in Athena.")
            
            if "Presto" in glue_table['ViewOriginalText']:
                table.viewOriginalText, table.viewExpandedText = HiveMappers.map_presto_view(table.tableName, glue_table['ViewOriginalText'], catalog)
            else:
                # Consider it as Hive view.
                table.viewExpandedText = glue_table['ViewExpandedText']
                table.parameters = {
                    "comment": "Presto View",
                    "presto_view": "true"
                }
                table.viewOriginalText = HiveMappers.map_hive_view(glue_table['ViewOriginalText'], catalog, glue_table['DatabaseName'], glue_table['StorageDescriptor']['Columns'])
        
        # Map the storage description
        sd = ttypes.StorageDescriptor(
            cols=[
                ttypes.FieldSchema(
                    name=rec['Name'],
                    type=rec['Type']
                ) for rec in glue_table['StorageDescriptor']['Columns']
            ],
            location=glue_table['StorageDescriptor'].get('Location'),
            inputFormat=glue_table['StorageDescriptor'].get('InputFormat'),
            outputFormat=glue_table['StorageDescriptor'].get('OutputFormat'),
            compressed=glue_table['StorageDescriptor'].get('Compressed'),
            numBuckets=glue_table['StorageDescriptor'].get('NumberOfBuckets', -1),
            serdeInfo=ttypes.SerDeInfo(
                serializationLib=glue_table['StorageDescriptor'].get('SerdeInfo', {}).get('SerializationLibrary', ''),
                parameters=glue_table['StorageDescriptor'].get('SerdeInfo', {}).get('Parameters', {}),
            ),
            bucketCols=glue_table['StorageDescriptor'].get('BucketColumns', []),
            sortCols=glue_table['StorageDescriptor'].get('SortColumns', []),
            parameters=glue_table['StorageDescriptor'].get('Parameters', {}),
            skewedInfo=ttypes.SkewedInfo(
                skewedColNames=glue_table['StorageDescriptor'].get('SkewedInfo', {}).get('SkewedColumnNames', []),
                skewedColValues=glue_table['StorageDescriptor'].get('SkewedInfo', {}).get('SkewedColumnValues', []),
                skewedColValueLocationMaps=(
                    glue_table['StorageDescriptor']
                    .get('SkewedInfo', {})
                    .get('SkewedColumnValueLocationMaps', {})
                ),
            ),
            storedAsSubDirectories=glue_table['StorageDescriptor'].get('StoredAsSubDirectories'),
        )
        table.sd = sd

        return table

    @staticmethod
    def map_glue_partition_for_table(databaseName, tableName, glue_partition):
        hive_partition = ttypes.Partition(
            values=glue_partition.get('Values'),
            dbName=databaseName,
            tableName=tableName,
            createTime=HiveMappers.unix_epoch_as_int(glue_partition.get('CreationTime', None)),
            lastAccessTime=HiveMappers.unix_epoch_as_int(glue_partition.get('LastAccessTime', None)),
            parameters=glue_partition.get('Parameters', {})
        )
        sd = ttypes.StorageDescriptor(
            cols=[
                ttypes.FieldSchema(
                    name=rec['Name'],
                    type=rec['Type']
                ) for rec in glue_partition['StorageDescriptor']['Columns']
            ],
            location=glue_partition['StorageDescriptor'].get('Location'),
            inputFormat=glue_partition['StorageDescriptor'].get('InputFormat'),
            outputFormat=glue_partition['StorageDescriptor'].get('OutputFormat'),
            compressed=glue_partition['StorageDescriptor'].get('Compressed'),
            numBuckets=glue_partition['StorageDescriptor'].get('NumberOfBuckets', -1),
            serdeInfo=ttypes.SerDeInfo(
                serializationLib=glue_partition['StorageDescriptor'].get('SerdeInfo', {}).get('SerializationLibrary', ''),
                parameters=glue_partition['StorageDescriptor'].get('SerdeInfo', {}).get('Parameters', {}),
            ),
            bucketCols=glue_partition['StorageDescriptor'].get('BucketColumns', []),
            sortCols=glue_partition['StorageDescriptor'].get('SortColumns', []),
            parameters=glue_partition['StorageDescriptor'].get('Parameters', {}),
            skewedInfo=ttypes.SkewedInfo(
                skewedColNames=glue_partition['StorageDescriptor'].get('SkewedInfo', {}).get('SkewedColumnNames', []),
                skewedColValues=glue_partition['StorageDescriptor'].get('SkewedInfo', {}).get('SkewedColumnValues', []),
                skewedColValueLocationMaps=(
                    glue_partition['StorageDescriptor']
                    .get('SkewedInfo', {})
                    .get('SkewedColumnValueLocationMaps', {})
                ),
            ),
            storedAsSubDirectories=glue_partition['StorageDescriptor'].get('StoredAsSubDirectories'),
        )
        hive_partition.sd = sd

        return hive_partition

    @staticmethod
    def unix_epoch_as_int(datetime_obj):
        if datetime_obj is not None:
            # For the spilled content, it is already converted to Int.
            if isinstance(datetime_obj, int):
                return datetime_obj
            else:
                return int(time.mktime(datetime_obj.timetuple()))
        else:
            return 0
