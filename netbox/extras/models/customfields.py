import re
from datetime import datetime, date

import django_filters
from django import forms
from django.contrib.contenttypes.models import ContentType
from django.contrib.postgres.fields import ArrayField
from django.core.validators import RegexValidator, ValidationError
from django.db import models
from django.urls import reverse
from django.utils.html import escape
from django.utils.safestring import mark_safe

from extras.choices import *
from extras.utils import FeatureQuery
from netbox.models import ChangeLoggedModel
from netbox.models.features import CloningMixin, ExportTemplatesMixin, WebhooksMixin
from utilities import filters
from utilities.forms import (
    CSVChoiceField, CSVMultipleChoiceField, DatePicker, DynamicModelChoiceField, DynamicModelMultipleChoiceField,
    JSONField, LaxURLField, StaticSelectMultiple, StaticSelect, add_blank_choice,
)
from utilities.querysets import RestrictedQuerySet
from utilities.validators import validate_regex


__all__ = (
    'CustomField',
    'CustomFieldManager',
)


class CustomFieldManager(models.Manager.from_queryset(RestrictedQuerySet)):
    use_in_migrations = True

    def get_for_model(self, model):
        """
        Return all CustomFields assigned to the given model.
        """
        content_type = ContentType.objects.get_for_model(model._meta.concrete_model)
        return self.get_queryset().filter(content_types=content_type)


class CustomField(CloningMixin, ExportTemplatesMixin, WebhooksMixin, ChangeLoggedModel):
    content_types = models.ManyToManyField(
        to=ContentType,
        related_name='custom_fields',
        limit_choices_to=FeatureQuery('custom_fields'),
        help_text='The object(s) to which this field applies.'
    )
    type = models.CharField(
        max_length=50,
        choices=CustomFieldTypeChoices,
        default=CustomFieldTypeChoices.TYPE_TEXT,
        help_text='The type of data this custom field holds'
    )
    object_type = models.ForeignKey(
        to=ContentType,
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        help_text='The type of NetBox object this field maps to (for object fields)'
    )
    name = models.CharField(
        max_length=50,
        unique=True,
        help_text='Internal field name',
        validators=(
            RegexValidator(
                regex=r'^[a-z0-9_]+$',
                message="Only alphanumeric characters and underscores are allowed.",
                flags=re.IGNORECASE
            ),
        )
    )
    label = models.CharField(
        max_length=50,
        blank=True,
        help_text='Name of the field as displayed to users (if not provided, '
                  'the field\'s name will be used)'
    )
    group_name = models.CharField(
        max_length=50,
        blank=True,
        help_text="Custom fields within the same group will be displayed together"
    )
    description = models.CharField(
        max_length=200,
        blank=True
    )
    required = models.BooleanField(
        default=False,
        help_text='If true, this field is required when creating new objects '
                  'or editing an existing object.'
    )
    filter_logic = models.CharField(
        max_length=50,
        choices=CustomFieldFilterLogicChoices,
        default=CustomFieldFilterLogicChoices.FILTER_LOOSE,
        help_text='Loose matches any instance of a given string; exact '
                  'matches the entire field.'
    )
    default = models.JSONField(
        blank=True,
        null=True,
        help_text='Default value for the field (must be a JSON value). Encapsulate '
                  'strings with double quotes (e.g. "Foo").'
    )
    weight = models.PositiveSmallIntegerField(
        default=100,
        help_text='Fields with higher weights appear lower in a form.'
    )
    validation_minimum = models.IntegerField(
        blank=True,
        null=True,
        verbose_name='Minimum value',
        help_text='Minimum allowed value (for numeric fields)'
    )
    validation_maximum = models.IntegerField(
        blank=True,
        null=True,
        verbose_name='Maximum value',
        help_text='Maximum allowed value (for numeric fields)'
    )
    validation_regex = models.CharField(
        blank=True,
        validators=[validate_regex],
        max_length=500,
        verbose_name='Validation regex',
        help_text='Regular expression to enforce on text field values. Use ^ and $ to force matching of entire string. '
                  'For example, <code>^[A-Z]{3}$</code> will limit values to exactly three uppercase letters.'
    )
    choices = ArrayField(
        base_field=models.CharField(max_length=100),
        blank=True,
        null=True,
        help_text='Comma-separated list of available choices (for selection fields)'
    )
    ui_visibility = models.CharField(
        max_length=50,
        choices=CustomFieldVisibilityChoices,
        default=CustomFieldVisibilityChoices.VISIBILITY_READ_WRITE,
        verbose_name='UI visibility',
        help_text='Specifies the visibility of custom field in the UI'
    )

    objects = CustomFieldManager()

    clone_fields = (
        'content_types', 'type', 'object_type', 'group_name', 'description', 'required', 'filter_logic', 'default',
        'weight', 'validation_minimum', 'validation_maximum', 'validation_regex', 'choices', 'ui_visibility',
    )

    class Meta:
        ordering = ['group_name', 'weight', 'name']

    def __str__(self):
        return self.label or self.name.replace('_', ' ').capitalize()

    def get_absolute_url(self):
        return reverse('extras:customfield', args=[self.pk])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Cache instance's original name so we can check later whether it has changed
        self._name = self.name

    def populate_initial_data(self, content_types):
        """
        Populate initial custom field data upon either a) the creation of a new CustomField, or
        b) the assignment of an existing CustomField to new object types.
        """
        for ct in content_types:
            model = ct.model_class()
            instances = model.objects.exclude(**{f'custom_field_data__contains': self.name})
            for instance in instances:
                instance.custom_field_data[self.name] = self.default
            model.objects.bulk_update(instances, ['custom_field_data'], batch_size=100)

    def remove_stale_data(self, content_types):
        """
        Delete custom field data which is no longer relevant (either because the CustomField is
        no longer assigned to a model, or because it has been deleted).
        """
        for ct in content_types:
            model = ct.model_class()
            instances = model.objects.filter(**{f'custom_field_data__{self.name}__isnull': False})
            for instance in instances:
                del instance.custom_field_data[self.name]
            model.objects.bulk_update(instances, ['custom_field_data'], batch_size=100)

    def rename_object_data(self, old_name, new_name):
        """
        Called when a CustomField has been renamed. Updates all assigned object data.
        """
        for ct in self.content_types.all():
            model = ct.model_class()
            params = {f'custom_field_data__{old_name}__isnull': False}
            instances = model.objects.filter(**params)
            for instance in instances:
                instance.custom_field_data[new_name] = instance.custom_field_data.pop(old_name)
            model.objects.bulk_update(instances, ['custom_field_data'], batch_size=100)

    def clean(self):
        super().clean()

        # Validate the field's default value (if any)
        if self.default is not None:
            try:
                if self.type in (CustomFieldTypeChoices.TYPE_TEXT, CustomFieldTypeChoices.TYPE_LONGTEXT):
                    default_value = str(self.default)
                else:
                    default_value = self.default
                self.validate(default_value)
            except ValidationError as err:
                raise ValidationError({
                    'default': f'Invalid default value "{self.default}": {err.message}'
                })

        # Minimum/maximum values can be set only for numeric fields
        if self.validation_minimum is not None and self.type != CustomFieldTypeChoices.TYPE_INTEGER:
            raise ValidationError({
                'validation_minimum': "A minimum value may be set only for numeric fields"
            })
        if self.validation_maximum is not None and self.type != CustomFieldTypeChoices.TYPE_INTEGER:
            raise ValidationError({
                'validation_maximum': "A maximum value may be set only for numeric fields"
            })

        # Regex validation can be set only for text fields
        regex_types = (
            CustomFieldTypeChoices.TYPE_TEXT,
            CustomFieldTypeChoices.TYPE_LONGTEXT,
            CustomFieldTypeChoices.TYPE_URL,
        )
        if self.validation_regex and self.type not in regex_types:
            raise ValidationError({
                'validation_regex': "Regular expression validation is supported only for text and URL fields"
            })

        # Choices can be set only on selection fields
        if self.choices and self.type not in (
                CustomFieldTypeChoices.TYPE_SELECT,
                CustomFieldTypeChoices.TYPE_MULTISELECT
        ):
            raise ValidationError({
                'choices': "Choices may be set only for custom selection fields."
            })

        # A selection field must have at least two choices defined
        if self.type == CustomFieldTypeChoices.TYPE_SELECT and self.choices and len(self.choices) < 2:
            raise ValidationError({
                'choices': "Selection fields must specify at least two choices."
            })

        # A selection field's default (if any) must be present in its available choices
        if self.type == CustomFieldTypeChoices.TYPE_SELECT and self.default and self.default not in self.choices:
            raise ValidationError({
                'default': f"The specified default value ({self.default}) is not listed as an available choice."
            })

        # Object fields must define an object_type; other fields must not
        if self.type in (CustomFieldTypeChoices.TYPE_OBJECT, CustomFieldTypeChoices.TYPE_MULTIOBJECT):
            if not self.object_type:
                raise ValidationError({
                    'object_type': "Object fields must define an object type."
                })
        elif self.object_type:
            raise ValidationError({
                'object_type': f"{self.get_type_display()} fields may not define an object type."
            })

    def serialize(self, value):
        """
        Prepare a value for storage as JSON data.
        """
        if value is None:
            return value
        if self.type == CustomFieldTypeChoices.TYPE_OBJECT:
            return value.pk
        if self.type == CustomFieldTypeChoices.TYPE_MULTIOBJECT:
            return [obj.pk for obj in value] or None
        return value

    def deserialize(self, value):
        """
        Convert JSON data to a Python object suitable for the field type.
        """
        if value is None:
            return value
        if self.type == CustomFieldTypeChoices.TYPE_OBJECT:
            model = self.object_type.model_class()
            return model.objects.filter(pk=value).first()
        if self.type == CustomFieldTypeChoices.TYPE_MULTIOBJECT:
            model = self.object_type.model_class()
            return model.objects.filter(pk__in=value)
        return value

    def to_form_field(self, set_initial=True, enforce_required=True, enforce_visibility=True, for_csv_import=False):
        """
        Return a form field suitable for setting a CustomField's value for an object.

        set_initial: Set initial data for the field. This should be False when generating a field for bulk editing.
        enforce_required: Honor the value of CustomField.required. Set to False for filtering/bulk editing.
        enforce_visibility: Honor the value of CustomField.ui_visibility. Set to False for filtering.
        for_csv_import: Return a form field suitable for bulk import of objects in CSV format.
        """
        initial = self.default if set_initial else None
        required = self.required if enforce_required else False

        # Integer
        if self.type == CustomFieldTypeChoices.TYPE_INTEGER:
            field = forms.IntegerField(
                required=required,
                initial=initial,
                min_value=self.validation_minimum,
                max_value=self.validation_maximum
            )

        # Boolean
        elif self.type == CustomFieldTypeChoices.TYPE_BOOLEAN:
            choices = (
                (None, '---------'),
                (True, 'True'),
                (False, 'False'),
            )
            field = forms.NullBooleanField(
                required=required, initial=initial, widget=StaticSelect(choices=choices)
            )

        # Date
        elif self.type == CustomFieldTypeChoices.TYPE_DATE:
            field = forms.DateField(required=required, initial=initial, widget=DatePicker())

        # Select
        elif self.type in (CustomFieldTypeChoices.TYPE_SELECT, CustomFieldTypeChoices.TYPE_MULTISELECT):
            choices = [(c, c) for c in self.choices]
            default_choice = self.default if self.default in self.choices else None

            if not required or default_choice is None:
                choices = add_blank_choice(choices)

            # Set the initial value to the first available choice (if any)
            if set_initial and default_choice:
                initial = default_choice

            if self.type == CustomFieldTypeChoices.TYPE_SELECT:
                field_class = CSVChoiceField if for_csv_import else forms.ChoiceField
                field = field_class(
                    choices=choices, required=required, initial=initial, widget=StaticSelect()
                )
            else:
                field_class = CSVMultipleChoiceField if for_csv_import else forms.MultipleChoiceField
                field = field_class(
                    choices=choices, required=required, initial=initial, widget=StaticSelectMultiple()
                )

        # URL
        elif self.type == CustomFieldTypeChoices.TYPE_URL:
            field = LaxURLField(required=required, initial=initial)

        # JSON
        elif self.type == CustomFieldTypeChoices.TYPE_JSON:
            field = JSONField(required=required, initial=initial)

        # Object
        elif self.type == CustomFieldTypeChoices.TYPE_OBJECT:
            model = self.object_type.model_class()
            field = DynamicModelChoiceField(
                queryset=model.objects.all(),
                required=required,
                initial=initial
            )

        # Multiple objects
        elif self.type == CustomFieldTypeChoices.TYPE_MULTIOBJECT:
            model = self.object_type.model_class()
            field = DynamicModelMultipleChoiceField(
                queryset=model.objects.all(),
                required=required,
                initial=initial
            )

        # Text
        else:
            widget = forms.Textarea if self.type == CustomFieldTypeChoices.TYPE_LONGTEXT else None
            field = forms.CharField(required=required, initial=initial, widget=widget)
            if self.validation_regex:
                field.validators = [
                    RegexValidator(
                        regex=self.validation_regex,
                        message=mark_safe(f"Values must match this regex: <code>{self.validation_regex}</code>")
                    )
                ]

        field.model = self
        field.label = str(self)
        if self.description:
            field.help_text = escape(self.description)

        # Annotate read-only fields
        if enforce_visibility and self.ui_visibility == CustomFieldVisibilityChoices.VISIBILITY_READ_ONLY:
            field.disabled = True
            prepend = '<br />' if field.help_text else ''
            field.help_text += f'{prepend}<i class="mdi mdi-alert-circle-outline"></i> Field is set to read-only.'

        return field

    def to_filter(self, lookup_expr=None):
        """
        Return a django_filters Filter instance suitable for this field type.

        :param lookup_expr: Custom lookup expression (optional)
        """
        kwargs = {
            'field_name': f'custom_field_data__{self.name}'
        }
        if lookup_expr is not None:
            kwargs['lookup_expr'] = lookup_expr

        # Text/URL
        if self.type in (
                CustomFieldTypeChoices.TYPE_TEXT,
                CustomFieldTypeChoices.TYPE_LONGTEXT,
                CustomFieldTypeChoices.TYPE_URL,
        ):
            filter_class = filters.MultiValueCharFilter
            if self.filter_logic == CustomFieldFilterLogicChoices.FILTER_LOOSE:
                kwargs['lookup_expr'] = 'icontains'

        # Integer
        elif self.type == CustomFieldTypeChoices.TYPE_INTEGER:
            filter_class = filters.MultiValueNumberFilter

        # Boolean
        elif self.type == CustomFieldTypeChoices.TYPE_BOOLEAN:
            filter_class = django_filters.BooleanFilter

        # Date
        elif self.type == CustomFieldTypeChoices.TYPE_DATE:
            filter_class = filters.MultiValueDateFilter

        # Select
        elif self.type == CustomFieldTypeChoices.TYPE_SELECT:
            filter_class = filters.MultiValueCharFilter

        # Multiselect
        elif self.type == CustomFieldTypeChoices.TYPE_MULTISELECT:
            filter_class = filters.MultiValueCharFilter
            kwargs['lookup_expr'] = 'has_key'

        # Object
        elif self.type == CustomFieldTypeChoices.TYPE_OBJECT:
            filter_class = filters.MultiValueNumberFilter

        # Multi-object
        elif self.type == CustomFieldTypeChoices.TYPE_MULTIOBJECT:
            filter_class = filters.MultiValueNumberFilter
            kwargs['lookup_expr'] = 'contains'

        # Unsupported custom field type
        else:
            return None

        filter_instance = filter_class(**kwargs)
        filter_instance.custom_field = self

        return filter_instance

    def validate(self, value):
        """
        Validate a value according to the field's type validation rules.
        """
        if value not in [None, '']:

            # Validate text field
            if self.type in (CustomFieldTypeChoices.TYPE_TEXT, CustomFieldTypeChoices.TYPE_LONGTEXT):
                if type(value) is not str:
                    raise ValidationError(f"Value must be a string.")
                if self.validation_regex and not re.match(self.validation_regex, value):
                    raise ValidationError(f"Value must match regex '{self.validation_regex}'")

            # Validate integer
            if self.type == CustomFieldTypeChoices.TYPE_INTEGER:
                if type(value) is not int:
                    raise ValidationError("Value must be an integer.")
                if self.validation_minimum is not None and value < self.validation_minimum:
                    raise ValidationError(f"Value must be at least {self.validation_minimum}")
                if self.validation_maximum is not None and value > self.validation_maximum:
                    raise ValidationError(f"Value must not exceed {self.validation_maximum}")

            # Validate boolean
            if self.type == CustomFieldTypeChoices.TYPE_BOOLEAN and value not in [True, False, 1, 0]:
                raise ValidationError("Value must be true or false.")

            # Validate date
            if self.type == CustomFieldTypeChoices.TYPE_DATE:
                if type(value) is not date:
                    try:
                        datetime.strptime(value, '%Y-%m-%d')
                    except ValueError:
                        raise ValidationError("Date values must be in the format YYYY-MM-DD.")

            # Validate selected choice
            if self.type == CustomFieldTypeChoices.TYPE_SELECT:
                if value not in self.choices:
                    raise ValidationError(
                        f"Invalid choice ({value}). Available choices are: {', '.join(self.choices)}"
                    )

            # Validate all selected choices
            if self.type == CustomFieldTypeChoices.TYPE_MULTISELECT:
                if not set(value).issubset(self.choices):
                    raise ValidationError(
                        f"Invalid choice(s) ({', '.join(value)}). Available choices are: {', '.join(self.choices)}"
                    )

        elif self.required:
            raise ValidationError("Required field cannot be empty.")
