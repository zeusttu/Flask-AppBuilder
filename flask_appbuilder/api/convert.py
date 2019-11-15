from marshmallow import fields
from marshmallow_enum import EnumField
from marshmallow_jsonapi import (
    fields as jsonapi_fields,
    Schema as JsonApiSchema,
    SchemaOpts as JsonApiSchemaOpts,
)
from marshmallow_sqlalchemy import field_for
from marshmallow_sqlalchemy.schema import ModelSchema, ModelSchemaOpts

from .util import jsonapi_requested


class TreeNode:
    def __init__(self, data):
        self.data = data
        self.childs = list()

    def __repr__(self):
        return "{}.{}".format(self.data, str(self.childs))


class Tree:
    """
        Simplistic one level Tree
    """

    def __init__(self):
        self.root = TreeNode("+")

    def add(self, data):
        node = TreeNode(data)
        self.root.childs.append(node)

    def add_child(self, parent, data):
        node = TreeNode(data)
        for n in self.root.childs:
            if n.data == parent:
                n.childs.append(node)
                return
        root = TreeNode(parent)
        self.root.childs.append(root)
        root.childs.append(node)

    def __repr__(self):
        ret = ""
        for node in self.root.childs:
            ret += str(node)
        return ret


def columns2Tree(columns):
    tree = Tree()
    for column in columns:
        if "." in column:
            tree.add_child(column.split(".")[0], column.split(".")[1])
        else:
            tree.add(column)
    return tree


class BaseModel2SchemaConverter(object):
    def __init__(self, datamodel, validators_columns):
        """
        :param datamodel: SQLAInterface
        """
        self.datamodel = datamodel
        self.validators_columns = validators_columns

    def convert(self, columns, **kwargs):
        pass


class JsonApiSqlAlchemySchemaOpts(ModelSchemaOpts, JsonApiSchemaOpts):
    """
    A schema options class necessary to make marshmallow-sqlalchemy and
    marshmallow-jsonapi play nice together.
    """


class Model2SchemaConverter(BaseModel2SchemaConverter):
    """
        Class that converts Models to marshmallow Schemas
    """

    def __init__(self, datamodel, validators_columns):
        """
        :param datamodel: SQLAInterface
        """
        super(Model2SchemaConverter, self).__init__(datamodel, validators_columns)

    @staticmethod
    def _debug_schema(schema):
        for k, v in schema._declared_fields.items():
            print(k, v)

    def _meta_schema_factory(self, columns, model, class_mixin):
        """
            Creates ModelSchema marshmallow-sqlalchemy

        :param columns: a list of columns to mix
        :param model: Model
        :param class_mixin: a marshamallow Schema to mix
        :return: ModelSchema
        """
        _model = model
        attrs = {}
        meta_attrs = {
            "model": _model,
            "strict": True,
            "sqla_session": self.datamodel.session,
        }
        parent_classes = (ModelSchema, class_mixin)
        if columns:
            meta_attrs["fields"] = columns
        if jsonapi_requested():
            meta_attrs["type_"] = _model.__name__
            attrs["OPTIONS_CLASS"] = JsonApiSqlAlchemySchemaOpts
            parent_classes = (ModelSchema, JsonApiSchema, class_mixin)
            if columns:
                primary_keys = self.datamodel.get_primary_key_columns()
                columns.extend(col for col in primary_keys if col not in columns)
        attrs["Meta"] = type("Meta", (), meta_attrs)
        return type("MetaSchema", parent_classes, attrs)

    def _column2field(self, datamodel, column, nested=True, enum_dump_by_name=False):
        """

        :param datamodel: SQLAInterface
        :param column: TreeNode column (childs are dotted column)
        :param nested: Boolean if will create nested fields
        :param enum_dump_by_name:
        :return: Schema.field
        """
        _model = datamodel.obj
        fields_module = jsonapi_fields if jsonapi_requested() else fields
        # Handle relations
        if datamodel.is_relation(column.data) and nested:
            required = not datamodel.is_nullable(column.data)
            nested_model = datamodel.get_related_model(column.data)
            lst = [item.data for item in column.childs]
            nested_schema = self.convert(lst, nested_model, nested=False)
            if datamodel.is_relation_many_to_one(column.data):
                many = False
            elif datamodel.is_relation_many_to_many(column.data):
                many = True
                required = False
            else:
                many = False
            field = fields_module.Nested(nested_schema, many=many, required=required)
            field.unique = datamodel.is_unique(column.data)
            return field
        # Handle bug on marshmallow-sqlalchemy #163
        elif datamodel.is_relation(column.data):
            if datamodel.is_relation_many_to_many(
                column.data
            ) or datamodel.is_relation_one_to_many(column.data):
                if datamodel.get_info(column.data).get("required", False):
                    required = True
                else:
                    required = False
            else:
                required = not datamodel.is_nullable(column.data)
            field = field_for(_model, column.data)
            field.required = required
            field.unique = datamodel.is_unique(column.data)
            return field
        # Handle Enums
        elif datamodel.is_enum(column.data):
            required = not datamodel.is_nullable(column.data)
            enum_class = datamodel.list_columns[column.data].info.get(
                "enum_class", datamodel.list_columns[column.data].type
            )
            if enum_dump_by_name:
                enum_dump_by = EnumField.NAME
            else:
                enum_dump_by = EnumField.VALUE
            field = EnumField(enum_class, dump_by=enum_dump_by, required=required)
            field.unique = datamodel.is_unique(column.data)
            return field
        # is custom property method field?
        if hasattr(getattr(_model, column.data), "fget"):
            return fields_module.Raw(dump_only=True)
        # is a normal model field not a function?
        if not hasattr(getattr(_model, column.data), "__call__"):
            field = field_for(_model, column.data)
            field.unique = datamodel.is_unique(column.data)
            if column.data in self.validators_columns:
                field.validate.append(self.validators_columns[column.data])
            return field

    @staticmethod
    def get_column_child_model(column):
        if "." in column:
            return column.split(".")[0]
        return column

    @staticmethod
    def is_column_dotted(column):
        return "." in column

    def convert(self, columns, model=None, nested=True, enum_dump_by_name=False):
        """
            Creates a Marshmallow ModelSchema class


        :param columns: List with columns to include, if empty converts all on model
        :param model: Override Model to convert
        :param nested: Generate relation with nested schemas
        :return: ModelSchema object
        """
        super(Model2SchemaConverter, self).convert(columns, model=model, nested=nested)

        class SchemaMixin:
            pass

        _model = model or self.datamodel.obj
        _datamodel = self.datamodel.__class__(_model)

        ma_sqla_fields_override = {}

        _columns = list()
        tree_columns = columns2Tree(columns)
        for column in tree_columns.root.childs:
            # Get child model is column is dotted notation
            ma_sqla_fields_override[column.data] = self._column2field(
                _datamodel, column, nested, enum_dump_by_name
            )
            _columns.append(column.data)
        for k, v in ma_sqla_fields_override.items():
            setattr(SchemaMixin, k, v)
        return self._meta_schema_factory(_columns, _model, SchemaMixin)()
