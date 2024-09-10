# SPDX-License-Identifier: BSD-2-Clause
# Copyright  (c) 2020-2023, The Chancellor, Masters and Scholars of the University
# of Oxford, and the 'Galv' Developers. All rights reserved.
from __future__ import annotations

import json
import os.path
from pathlib import Path
import re
import tempfile
from typing import Optional, Union

import jsonschema
from django.conf import settings
from django.core.files import File
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.db import transaction
from drf_spectacular.types import OpenApiTypes
from rest_framework.reverse import reverse
from drf_spectacular.utils import extend_schema_field, extend_schema_serializer, OpenApiExample
from rest_framework.exceptions import ValidationError
from rest_framework.status import HTTP_403_FORBIDDEN

from ..models import Harvester, \
    HarvesterEnvVar, \
    HarvestError, \
    MonitoredPath, \
    ObservedFile, \
    Cell, \
    Equipment, \
    DataUnit, \
    DataColumnType, \
    KnoxAuthToken, CellFamily, EquipmentTypes, CellFormFactors, CellChemistries, CellModels, CellManufacturers, \
    EquipmentManufacturers, EquipmentModels, EquipmentFamily, Schedule, ScheduleIdentifiers, CyclerTest, \
    render_pybamm_schedule, ScheduleFamily, ValidationSchema, Experiment, Lab, Team, GroupProxy, UserProxy, \
    SchemaValidation, UserActivation, UserLevel, ALLOWED_USER_LEVELS_READ, ALLOWED_USER_LEVELS_EDIT, \
    ALLOWED_USER_LEVELS_DELETE, ALLOWED_USER_LEVELS_EDIT_PATH, ArbitraryFile, ParquetPartition, ColumnMapping, \
    get_user_auth_details, GalvStorageType, AdditionalS3StorageType, PasswordReset, StorageError, FileState
from ..models.utils import ScheduleRenderError
from django.utils import timezone
from django.conf.global_settings import DATA_UPLOAD_MAX_MEMORY_SIZE
from rest_framework import serializers
from knox.models import AuthToken

from galv_harvester.harvest import InternalHarvestProcessor

from .utils import CustomPropertiesModelSerializer, GetOrCreateTextField, augment_extra_kwargs, url_help_text, \
    PermissionsMixin, TruncatedUserHyperlinkedRelatedIdField, \
    TruncatedHyperlinkedRelatedIdField, \
    CreateOnlyMixin, ValidationPresentationMixin, PasswordField


@extend_schema_serializer(examples = [
    OpenApiExample(
        'Valid example',
        summary='User details',
        description='Full details are only available to the user themselves, or to superusers',
        value={
            "username": "admin",
            "email": "admin@galv.ox",
            "first_name": "Adam",
            "last_name": "Minotaur",
            "url": "http://localhost:8001/users/1/",
            "id": 1,
            "is_staff": True,
            "is_superuser": True,
            "is_lab_admin": True,
            "groups": [
                "http://localhost:8001/groups/1/",
                "http://localhost:8001/groups/2/"
            ],
            "permissions": {
                "create": True,
                "destroy": False,
                "write": True,
                "read": True
            }
        },
        response_only=True, # signal that example only applies to responses
    ),
])
class UserSerializer(serializers.HyperlinkedModelSerializer, PermissionsMixin):
    current_password = serializers.CharField(
        write_only=True,
        allow_blank=True,
        required=False,
        style={'input_type': 'password'},
        help_text="Current password"
    )
    is_lab_admin = serializers.SerializerMethodField()

    def get_is_lab_admin(self, instance) -> bool:
        return instance.groups.filter(editable_lab__isnull=False).exists()

    def validate_email(self, value):
        if self.instance and self.instance.email == value:
            return value
        if UserProxy.objects.filter(email=value).exists():
            raise ValidationError("Email address is already in use")
        return value

    @staticmethod
    def validate_password(value):
        if len(value) < 8:
            raise ValidationError("Password must be at least 8 characters")
        return value

    def validate(self, attrs):
        current_password = attrs.pop('current_password', None)
        if self.instance and not self.instance.check_password(current_password):
            raise ValidationError(f"Current password is incorrect")
        return attrs

    def create(self, validated_data):
        user = UserProxy.objects.create_user(**validated_data, is_active=False)
        activation = UserActivation.objects.create(user=user)
        activation.send_email(request=self.context['request'])
        return user

    def update(self, instance, validated_data):
        if 'password' in validated_data:
            instance.set_password(validated_data.pop('password'))
        return super().update(instance, validated_data)

    class Meta:
        model = UserProxy
        write_fields = ['username', 'email', 'first_name', 'last_name']
        write_only_fields = ['password', 'current_password']
        read_only_fields = ['url', 'id', 'is_staff', 'is_superuser', 'is_lab_admin', 'permissions']
        fields = [*write_fields, *read_only_fields, *write_only_fields]
        extra_kwargs = augment_extra_kwargs({
            'password': {'write_only': True, 'help_text': "Password (8 characters minimum)"},
        })


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField(help_text="Email address to send password reset link to")

    def validate_email(self, value):
        if not UserProxy.objects.filter(email=value).exists():
            raise ValidationError("No user with that email address")
        return value

    def reset(self, validated_data):
        user = UserProxy.objects.get(email=validated_data['email'])
        token = PasswordReset.objects.create(user=user)
        token.send_email()


class PasswordResetSerializer(serializers.Serializer):
    email = serializers.EmailField(help_text="Email address for user account")
    token = serializers.CharField(help_text="Token from password reset email")
    password = serializers.CharField(help_text="New password (8 characters minimum)")

    def validate_password(self, value):
        if len(value) < 8:
            raise ValidationError("Password must be at least 8 characters")
        return value

    def validate(self, attrs):
        try:
            user = UserProxy.objects.get(email=attrs['email'])
        except UserProxy.DoesNotExist:
            raise ValidationError("No user with that email address")
        try:
            token = PasswordReset.objects.get(user=user, token=attrs['token'])
        except PasswordReset.DoesNotExist:
            raise ValidationError("Invalid token")
        if token.expired:
            raise ValidationError("Token has expired")
        return attrs

    def reset(self, validated_data):
        user = UserProxy.objects.get(email=validated_data['email'])
        user.set_password(validated_data['password'])
        user.save()
        PasswordReset.objects.filter(user=user).delete()
        return {"success": True}


@extend_schema_serializer(examples = [
    OpenApiExample(
        'Valid example',
        summary='Group details',
        description='Groups are used to manage permissions for a set of users',
        value=[
            "http://localhost:8001/users/1/"
        ],
        response_only=True, # signal that example only applies to responses
    ),
])
class TransparentGroupSerializer(serializers.HyperlinkedModelSerializer, PermissionsMixin):
    users = TruncatedUserHyperlinkedRelatedIdField(
        UserSerializer,
        ['url', 'id', 'username', 'first_name', 'last_name', 'permissions'],
        view_name='userproxy-detail',
        queryset=UserProxy.objects.filter(is_active=True),
        read_only=False,
        source='user_set',
        many=True,
        help_text="Users in the group"
    )

    @staticmethod
    def validate_users(value):
        # Only active users can be added to groups
        return [u for u in value if u.is_active]

    def update(self, instance, validated_data):
        if 'user_set' in validated_data:
            # Check there will be at least one user left for lab admin groups
            if hasattr(instance, 'editable_lab'):
                if len(validated_data['user_set']) < 1:
                    raise ValidationError(f"Labs must always have at least one administrator")
            instance.user_set.set(validated_data.pop('user_set'))
        return instance

    def to_representation(self, instance) -> list[str]:
        ret = super().to_representation(instance)
        return ret['users']

    def to_internal_value(self, data):
        if isinstance(data, list):
            return super().to_internal_value({'users': data})
        return super().to_internal_value(data)

    class Meta:
        model = GroupProxy
        fields = ['users']

@extend_schema_serializer(examples = [
    OpenApiExample(
        'Valid example',
        summary='Team details',
        description='Teams are groups of users assigned to a project. They can easily create and share resources.',
        value={
            "url": "http://localhost:8001/teams/1/",
            "id": 1,
            "member_group": {
                "id": 3,
                "url": "http://localhost:8001/groups/3/",
                "name": "example_team_members",
                "users": [],
                "permissions": {
                    "create": False,
                    "destroy": False,
                    "write": True,
                    "read": True
                }
            },
            "admin_group": {
                "id": 2,
                "url": "http://localhost:8001/groups/2/",
                "name": "example_team_admins",
                "users": [
                    "http://localhost:8001/users/1/"
                ],
                "permissions": {
                    "create": False,
                    "destroy": False,
                    "write": True,
                    "read": True
                }
            },
            "monitored_paths": [],
            "cellfamily_resources": [
                "http://localhost:8001/cell_families/42fc4c44-efbb-4457-a734-f68ee28de617/",
                "http://localhost:8001/cell_families/5d19c8d6-a976-423d-ab5d-a624a0606d30/"
            ],
            "cell_resources": [
                "http://localhost:8001/cells/6a3a910b-d42e-46f6-9604-6fb3c2f3d059/",
                "http://localhost:8001/cells/4281a89b-48ff-4f4a-bcd8-5fe427f87a81/"
            ],
            "equipmentfamily_resources": [
                "http://localhost:8001/equipment_families/947e1f7c-c5b9-47b8-a121-d1e519a7154c/",
                "http://localhost:8001/equipment_families/6ef7c3b4-cb3b-421f-b6bf-de1e1acfaae8/"
            ],
            "equipment_resources": [
                "http://localhost:8001/equipment/a7bd4c43-29c7-40f1-bcf7-a2924ed474c2/",
                "http://localhost:8001/equipment/31fd16ef-0667-4a31-9232-b5a649913227/",
                "http://localhost:8001/equipment/12039516-72bf-42b7-a687-cb210ca4a087/"
            ],
            "schedulefamily_resources": [
                "http://localhost:8001/schedule_families/e25f7c94-ca32-4f47-b95a-3b0e7ae4a47f/"
            ],
            "schedule_resources": [
                "http://localhost:8001/schedules/5a2d7da9-393c-44ee-827a-5d15133c48d6/",
                "http://localhost:8001/schedules/7771fc54-7209-4564-9ec7-e87855f7ee67/"
            ],
            "cyclertest_resources": [
                "http://localhost:8001/cycler_tests/2b7313c9-94c2-4276-a4ee-e9d58d8a641b/",
                "http://localhost:8001/cycler_tests/e5a1a806-ef9e-4da8-9dd4-caa6cb491af9/"
            ],
            "experiment_resources": [],
            "permissions": {
                "create": True,
                "write": True,
                "read": True
            },
            "name": "Example Team",
            "description": "This Team exists to demonstrate the system.",
            "lab": "http://localhost:8001/labs/1/"
        },
        response_only=True, # signal that example only applies to responses
    ),
])
class TeamSerializer(serializers.HyperlinkedModelSerializer, PermissionsMixin):
    member_group = TransparentGroupSerializer(required=False, help_text="Members of this Team")
    admin_group = TransparentGroupSerializer(required=False, help_text="Administrators of this Team")
    cellfamily_resources = TruncatedHyperlinkedRelatedIdField(
        'CellFamilySerializer',
        ['manufacturer', 'model', 'chemistry', 'form_factor'],
        'cellfamily-detail',
        read_only=True,
        many=True,
        help_text="Cell Families belonging to this Team"
    )
    cell_resources = TruncatedHyperlinkedRelatedIdField(
        'CellSerializer',
        ['identifier', 'family'],
        'cell-detail',
        read_only=True,
        many=True,
        help_text="Cells belonging to this Team"
    )
    equipmentfamily_resources = TruncatedHyperlinkedRelatedIdField(
        'EquipmentFamilySerializer',
        ['type', 'manufacturer', 'model'],
        'equipmentfamily-detail',
        read_only=True,
        many=True,
        help_text="Equipment Families belonging to this Team"
    )
    equipment_resources = TruncatedHyperlinkedRelatedIdField(
        'EquipmentSerializer',
        ['identifier', 'family'],
        'equipment-detail',
        read_only=True,
        many=True,
        help_text="Equipment belonging to this Team"
    )
    schedulefamily_resources = TruncatedHyperlinkedRelatedIdField(
        'ScheduleFamilySerializer',
        ['identifier', ],
        'schedulefamily-detail',
        read_only=True,
        many=True,
        help_text="Schedule Families belonging to this Team"
    )
    schedule_resources = TruncatedHyperlinkedRelatedIdField(
        'ScheduleSerializer',
        ['family', ],
        'schedule-detail',
        read_only=True,
        many=True,
        help_text="Schedules belonging to this Team"
    )
    cyclertest_resources = TruncatedHyperlinkedRelatedIdField(
        'CyclerTestSerializer',
        ['cell', 'equipment', 'schedule'],
        'cyclertest-detail',
        read_only=True,
        many=True,
        help_text="Cycler Tests belonging to this Team"
    )
    experiment_resources = TruncatedHyperlinkedRelatedIdField(
        'ExperimentSerializer',
        ['title'],
        'experiment-detail',
        read_only=True,
        many=True,
        help_text="Experiments belonging to this Team"
    )
    lab = TruncatedHyperlinkedRelatedIdField(
        'LabSerializer',
        ['name'],
        'lab-detail',
        queryset=Lab.objects.all(),
        help_text="Lab this Team belongs to"
    )

    def validate_lab(self, value):
        """
        Only lab admins can create teams in their lab
        """
        try:
            assert value.pk in self.context['request'].user_auth_details.writeable_lab_ids
        except:
            raise ValidationError("You may only create Teams in your own lab(s)")
        return value

    def create(self, validated_data):
        admin_group = validated_data.pop('admin_group', [])
        member_group = validated_data.pop('member_group', [])
        if len(admin_group) == 0:
            try:
                admin_group = [self.context['request'].user.id]
            except KeyError:
                raise ValidationError("No admins specified and no request context available to determine user.")
        team = super().create(validated_data)
        TransparentGroupSerializer().update(team.admin_group, admin_group)
        TransparentGroupSerializer().update(team.member_group, member_group)
        team.save()
        return team

    def update(self, instance, validated_data):
        """
        Pass group updates to the group serializer
        """
        if 'admin_group' in validated_data:
            admin_group = validated_data.pop('admin_group')
            TransparentGroupSerializer().update(instance.admin_group, admin_group)
        if 'member_group' in validated_data:
            member_group = validated_data.pop('member_group')
            TransparentGroupSerializer().update(instance.member_group, member_group)
        return super().update(instance, validated_data)

    class Meta:
        model = Team
        read_only_fields = [
            'url', 'id',
            'monitored_paths',
            'cellfamily_resources', 'cell_resources',
            'equipmentfamily_resources', 'equipment_resources',
            'schedulefamily_resources', 'schedule_resources',
            'cyclertest_resources', 'experiment_resources',
            'permissions'
        ]
        fields = [*read_only_fields, 'name', 'description', 'lab', 'member_group', 'admin_group']


@extend_schema_serializer(examples = [
    OpenApiExample(
        'Valid example',
        summary='Galv Storage details',
        description='Galv Storage is storage allocated to a Lab by the Galv server',
        value={
            "url": "http://localhost:8001/galv_storage_type/1/",
            "id": "1",
            "name": "Example Lab Storage",
            "lab": "http://localhost:8001/labs/1/",
            "quota_bytes": "100000000",
            "bytes_used": "500234",
            "priority": "0",
            "enabled": "true",
            "permissions": {
                "create": True,
                "write": True,
                "read": True
            }
        },
        response_only=True, # signal that example only applies to responses
    ),
])
class GalvStorageTypeSerializer(serializers.HyperlinkedModelSerializer, PermissionsMixin):
    bytes_used = serializers.SerializerMethodField()

    def get_bytes_used(self, instance) -> int:
        return instance.get_bytes_used()

    class Meta:
        model = GalvStorageType
        fields = ['url', 'id', 'name', 'lab', 'quota_bytes', 'bytes_used', 'priority', 'enabled', 'permissions']
        read_only_fields = ['url', 'id', 'lab', 'quota_bytes', "permissions"]
        extra_kwargs = augment_extra_kwargs()


@extend_schema_serializer(examples = [
    OpenApiExample(
        'Valid example',
        summary='Additional Storage details',
        description='Additional Storage is storage configured by a Lab',
        value={
            "url": "http://localhost:8001/additional_storage_type/1/",
            "id": "1",
            "name": "Example Lab S3 Storage",
            "lab": "http://localhost:8001/labs/1/",
            "quota_bytes": "100000000",
            "bytes_used": "500234",
            "priority": "0",
            "enabled": "true",
            "access_key": "AWS_********",
            "secret_key": "********",
            "bucket_name": "eg_lab_bucket",
            "location": "galv-files",
            "custom_domain": "",
            "permissions": {
                "create": True,
                "write": True,
                "read": True
            }
        },
        response_only=True, # signal that example only applies to responses
    ),
])
class AdditionalS3StorageTypeSerializer(serializers.HyperlinkedModelSerializer, PermissionsMixin, CreateOnlyMixin):
    bytes_used = serializers.SerializerMethodField()
    secret_key = PasswordField(help_text="Secret key for S3 storage")
    access_key = PasswordField(show_first_chars=4, help_text="Access key for S3 storage")

    lab = TruncatedHyperlinkedRelatedIdField(
        'LabSerializer',
        ['name'],
        'lab-detail',
        queryset=Lab.objects.all(),
        help_text="Lab this Storage belongs to",
        create_only=True
    )

    def get_bytes_used(self, instance) -> int:
        return instance.get_bytes_used()

    def validate_lab(self, value):
        """
        Only lab admins can create teams in their lab
        """
        try:
            assert value.pk in self.context['request'].user_auth_details.writeable_lab_ids
        except:
            raise ValidationError("You may only create Storages in your own lab(s)")
        return value

    def validate_access_key(self, value):
        if self.instance is not None and value in [None, self.fields['access_key'].to_representation(self.instance.access_key)]:
            return self.instance.access_key
        if not value or not isinstance(value, str):
            raise ValidationError("access_key must be a string")
        return value

    def validate_secret_key(self, value):
        if self.instance is not None and value in [None, self.fields['secret_key'].to_representation(self.instance.secret_key)]:
            return self.instance.secret_key
        if not value or not isinstance(value, str):
            raise ValidationError("secret_key must be a string")
        return value

    class Meta:
        model = AdditionalS3StorageType
        fields = [
            'url', 'id',
            'name', 'lab', 'quota_bytes', 'bytes_used', 'priority', 'enabled',
            'secret_key', 'access_key', 'bucket_name', 'location', 'region_name', 'custom_domain',
            'permissions'
        ]
        read_only_fields = ['url', 'id', "permissions"]
        extra_kwargs = augment_extra_kwargs({'lab': {'create_only': True}})


@extend_schema_serializer(examples = [
    OpenApiExample(
        'Valid example',
        summary='Lab details',
        description='Labs are collections of teams, and are used to organise access to raw data.',
        value={
            "url": "http://localhost:8001/labs/1/",
            "id": 1,
            "name": "Example Lab",
            "description": "This Lab exists to demonstrate the system.",
            "admin_group": {
                "id": 1,
                "url": "http://localhost:8001/groups/1/",
                "name": "example_lab_admins",
                "users": [
                    "http://localhost:8001/users/1/"
                ],
                "permissions": {
                    "create": False,
                    "destroy": False,
                    "write": True,
                    "read": True
                }
            },
            "harvesters": [],
            "teams": [
                "http://localhost:8001/teams/1/"
            ],
            "permissions": {
                "create": True,
                "write": True,
                "read": True
            }
        },
        response_only=True, # signal that example only applies to responses
    ),
])
class LabSerializer(serializers.HyperlinkedModelSerializer, PermissionsMixin):
    admin_group = TransparentGroupSerializer(help_text="Group of users who can edit this Lab")
    teams = TruncatedHyperlinkedRelatedIdField(
        'TeamSerializer',
        ['name', 'admin_group', 'member_group'],
        'team-detail',
        read_only=True,
        many=True,
        help_text="Teams in this Lab"
    )
    storages = serializers.SerializerMethodField()

    def get_storages(self, instance) -> list[str]:
        from ..views import get_storage_url
        storages = instance.get_all_storage_types()
        return [
            get_storage_url(s._meta.model, 'detail', args=[s.pk], request=self.context['request'])
            for s in storages
        ]

    def create(self, validated_data):
        admin_group = validated_data.pop('admin_group')
        lab = super().create(validated_data)
        TransparentGroupSerializer().update(lab.admin_group, admin_group)
        GalvStorageType.objects.create(lab=lab, quota_bytes=settings.LAB_STORAGE_QUOTA_BYTES)
        lab.save()
        return lab

    def update(self, instance, validated_data):
        """
        Pass group updates to the group serializer
        """
        if 'admin_group' in validated_data:
            admin_group = validated_data.pop('admin_group')
            TransparentGroupSerializer().update(instance.admin_group, admin_group)
        return super().update(instance, validated_data)

    class Meta:
        model = Lab
        fields = [
            'url', 'id',
            'name', 'description',
            'admin_group',
            'harvesters',
            'teams',
            'storages',
            'permissions'
        ]
        read_only_fields = ['url', 'id', 'teams', 'harvesters', 'permissions']


class WithTeamMixin(serializers.Serializer):
    team = TruncatedHyperlinkedRelatedIdField(
        'TeamSerializer',
        ['name'],
        'team-detail',
        queryset=Team.objects.all(),
        help_text="Team this resource belongs to",
        allow_empty=True,
        allow_null=True
    )
    read_access_level = serializers.ChoiceField(
        choices=[(v.value, v.label) for v in ALLOWED_USER_LEVELS_READ],
        help_text="Minimum user level required to read this resource",
        allow_null=True,
        default=UserLevel.LAB_MEMBER.value
    )
    edit_access_level = serializers.ChoiceField(
        choices=[(v.value, v.label) for v in ALLOWED_USER_LEVELS_EDIT],
        help_text="Minimum user level required to edit this resource",
        allow_null=True,
        default=UserLevel.TEAM_MEMBER.value
    )
    delete_access_level = serializers.ChoiceField(
        choices=[(v.value, v.label) for v in ALLOWED_USER_LEVELS_DELETE],
        help_text="Minimum user level required to create this resource",
        allow_null=True,
        default=UserLevel.TEAM_MEMBER.value
    )

    def validate_team(self, value):
        """
        Only team members can create resources in their team.
        If a resource is being moved from one team to another, the user must be a member of both teams.
        """
        try:
            teams = Team.objects.filter(pk__in=self.context['request'].user_auth_details.team_ids)
            if value is None:
                if len(teams) == 1:
                    value = teams[0]
                else:
                    raise ValidationError("You must specify a team because you are a member of multiple teams")
            else:
                assert value in teams
        except KeyError:
            raise ValidationError("No request context available to determine user's teams")
        except:
            raise ValidationError("You may only create resources in your own team(s)", code=HTTP_403_FORBIDDEN)
        if self.instance is not None:
            try:
                assert self.instance.team in teams
            except:
                raise ValidationError("You may only edit resources in your own team(s)")
        else:
            assert value is not None
        return value

    def validate_access_level(self, value, allowed_values):
        try:
            v = UserLevel(value)
        except ValueError:
            raise ValidationError((
                f"Invalid access level '{value}'. "
                f"Expected one of {[v.value for v in allowed_values]}"
            ))
        if self.instance is not None:
            try:
                assert v in allowed_values
            except:
                raise ValidationError((
                    f"Invalid read access level '{value}'. "
                    f"Expected one of {[v.value for v in allowed_values]}"
                ))
        return v.value

    def validate_read_access_level(self, value):
        return self.validate_access_level(value, ALLOWED_USER_LEVELS_READ)

    def validate_edit_access_level(self, value):
        return self.validate_access_level(value, ALLOWED_USER_LEVELS_EDIT)

    def validate_delete_access_level(self, value):
        return self.validate_access_level(value, ALLOWED_USER_LEVELS_DELETE)

    def validate(self, attrs):
        """
        Only team members can change read and edit access levels.
        Only team admins can change delete access levels.
        Ensure access levels follow the hierarchy:
        READ <= EDIT <= DELETE
        """
        if self.instance is not None:
            # Remove unchanged access levels.
            # The frontend will send all access levels, even if they haven't changed,
            # so this is a convenience to prevent access denial when submitting unchanged data.
            for level in ['read_access_level', 'edit_access_level', 'delete_access_level']:
                if level in attrs and getattr(self.instance, level) == attrs[level]:
                    del attrs[level]
            user_access_level = self.instance.get_user_level(self.context['request'])
            if 'read_access_level' in attrs or 'edit_access_level' in attrs:
                if user_access_level < UserLevel.TEAM_MEMBER.value:
                    raise ValidationError("You may only change access levels if you are a team member")
                for access_level in ['read_access_level', 'edit_access_level']:
                    if access_level in attrs:
                        if getattr(self.instance, access_level) > user_access_level:
                            raise ValidationError(f"You may not change {access_level} because your access level is too low")
            if 'delete_access_level' in attrs:
                if user_access_level < UserLevel.TEAM_ADMIN.value:
                    raise ValidationError("You may only change delete access levels if you are a team admin")
        if 'read_access_level' in attrs:
            edit_level = attrs.get(
                'edit_access_level',
                self.instance.edit_access_level if self.instance else UserLevel.TEAM_ADMIN.value
            )
            if attrs['read_access_level'] > edit_level:
                raise ValidationError("Read access level must be less than or equal to edit access level")
        if 'edit_access_level' in attrs:
            delete_level = attrs.get(
                'delete_access_level',
                self.instance.delete_access_level if self.instance else UserLevel.TEAM_ADMIN.value
            )
            if attrs['edit_access_level'] > delete_level:
                raise ValidationError("Edit access level must be less than or equal to delete access level")
        return attrs

@extend_schema_serializer(examples = [
    OpenApiExample(
        'Valid example',
        summary='Cell details',
        description='Cells are the electrical energy storage devices used in cycler tests. They are grouped into families.',
        value={
            "url": "http://localhost:8001/cells/6a3a910b-d42e-46f6-9604-6fb3c2f3d059/",
            "id": "6a3a910b-d42e-46f6-9604-6fb3c2f3d059",
            "identifier": "sny-vtc-1234-xx94",
            "family": "http://localhost:8001/cell_families/42fc4c44-efbb-4457-a734-f68ee28de617/",
            "cycler_tests": [
                "http://localhost:8001/cycler_tests/2b7313c9-94c2-4276-a4ee-e9d58d8a641b/"
            ],
            "in_use": True,
            "team": "http://localhost:8001/teams/1/",
            "permissions": {
                "create": True,
                "destroy": True,
                "write": True,
                "read": True
            },
            "custom-property": "resources can have arbitrary additional JSON-serializable properties"
        },
        response_only=True, # signal that example only applies to responses
    ),
])
class CellSerializer(CustomPropertiesModelSerializer, PermissionsMixin, WithTeamMixin, ValidationPresentationMixin):
    family = TruncatedHyperlinkedRelatedIdField(
        'CellFamilySerializer',
        ['manufacturer', 'model', 'chemistry', 'form_factor'],
        'cellfamily-detail',
        queryset=CellFamily.objects.all(),
        help_text="Cell Family this Cell belongs to"
    )
    cycler_tests = TruncatedHyperlinkedRelatedIdField(
        'CyclerTestSerializer',
        ['equipment', 'schedule'],
        'cyclertest-detail',
        read_only=True,
        many=True,
        help_text="Cycler Tests using this Cell"
    )

    class Meta:
        model = Cell
        fields = [
            'url', 'id', 'identifier', 'family', 'cycler_tests', 'in_use', 'team',
            'permissions', 'read_access_level', 'edit_access_level', 'delete_access_level', 'custom_properties'
        ]
        read_only_fields = ['url', 'id', 'cycler_tests', 'in_use', 'permissions']


@extend_schema_serializer(examples = [
    OpenApiExample(
        'Valid example',
        summary='Cell Family details',
        description='Cell Families group together properties shared by multiple Cells of the same make and model.',
        value={
            "url": "http://localhost:8001/cell_families/5d19c8d6-a976-423d-ab5d-a624a0606d30/",
            "id": "5d19c8d6-a976-423d-ab5d-a624a0606d30",
            "manufacturer": "LG",
            "model": "HG2",
            "datasheet": None,
            "chemistry": "NMC",
            "nominal_voltage_v": 3.6,
            "nominal_capacity_ah": None,
            "initial_ac_impedance_o": None,
            "initial_dc_resistance_o": None,
            "energy_density_wh_per_kg": None,
            "power_density_w_per_kg": None,
            "form_factor": "Cyclindrical",
            "cells": [
                "http://localhost:8001/cells/4281a89b-48ff-4f4a-bcd8-5fe427f87a81/"
            ],
            "in_use": True,
            "team": "http://localhost:8001/teams/1/",
            "permissions": {
                "create": True,
                "destroy": True,
                "write": True,
                "read": True
            },
            "fast_charge_constant_current": 0.5,
            "fast_charge_constant_voltage": 4.2,
            "standard_discharge_constant_current": 1.0
        },
        response_only=True, # signal that example only applies to responses
    ),
])
class CellFamilySerializer(CustomPropertiesModelSerializer, PermissionsMixin, WithTeamMixin):
    manufacturer = GetOrCreateTextField(foreign_model=CellManufacturers, help_text="Manufacturer name")
    model = GetOrCreateTextField(foreign_model=CellModels, help_text="Model number")
    chemistry = GetOrCreateTextField(foreign_model=CellChemistries, help_text="Chemistry type")
    form_factor = GetOrCreateTextField(foreign_model=CellFormFactors, help_text="Physical form factor")
    cells = TruncatedHyperlinkedRelatedIdField(
        'CellSerializer',
        ['identifier'],
        'cell-detail',
        read_only=True,
        many=True,
        help_text="Cells belonging to this Cell Family"
    )

    class Meta:
        model = CellFamily
        fields = [
            'url',
            'id',
            'manufacturer',
            'model',
            'datasheet',
            'chemistry',
            'nominal_voltage_v',
            'nominal_capacity_ah',
            'initial_ac_impedance_o',
            'initial_dc_resistance_o',
            'energy_density_wh_per_kg',
            'power_density_w_per_kg',
            'form_factor',
            'cells',
            'in_use',
            'team',
            'permissions',
            'read_access_level',
            'edit_access_level',
            'delete_access_level',
            'custom_properties'
        ]
        read_only_fields = ['url', 'id', 'cells', 'in_use', 'permissions']


@extend_schema_serializer(examples = [
    OpenApiExample(
        'Valid example',
        summary='Equipment Family details',
        description='Equipment Families group together properties shared by multiple pieces of Equipment of the same make and model.',
        value={
            "url": "http://localhost:8001/equipment_families/947e1f7c-c5b9-47b8-a121-d1e519a7154c/",
            "id": "947e1f7c-c5b9-47b8-a121-d1e519a7154c",
            "type": "Thermal Chamber",
            "manufacturer": "Binder",
            "model": "KB115",
            "in_use": True,
            "team": "http://localhost:8001/teams/1/",
            "equipment": [
                "http://localhost:8001/equipment/a7bd4c43-29c7-40f1-bcf7-a2924ed474c2/",
                "http://localhost:8001/equipment/31fd16ef-0667-4a31-9232-b5a649913227/"
            ],
            "permissions": {
                "create": True,
                "destroy": False,
                "write": True,
                "read": True
            }
        },
        response_only=True, # signal that example only applies to responses
    ),
])
class EquipmentFamilySerializer(CustomPropertiesModelSerializer, PermissionsMixin, WithTeamMixin):
    type = GetOrCreateTextField(foreign_model=EquipmentTypes, help_text="Equipment type")
    manufacturer = GetOrCreateTextField(foreign_model=EquipmentManufacturers, help_text="Manufacturer name")
    model = GetOrCreateTextField(foreign_model=EquipmentModels, help_text="Model number")
    equipment = TruncatedHyperlinkedRelatedIdField(
        'EquipmentSerializer',
        ['identifier'],
        'equipment-detail',
        read_only=True,
        many=True,
        help_text="Equipment belonging to this Equipment Family"
    )

    class Meta:
        model = EquipmentFamily
        fields = [
            'url',
            'id',
            'type',
            'manufacturer',
            'model',
            'in_use',
            'team',
            'equipment',
            'permissions',
            'read_access_level',
            'edit_access_level',
            'delete_access_level',
            'custom_properties'
        ]
        read_only_fields = ['url', 'id', 'in_use', 'equipment', 'permissions']


@extend_schema_serializer(examples = [
    OpenApiExample(
        'Valid example',
        summary='Equipment details',
        description='Equipment is used to perform cycler tests. It includes cyclers themselves, as well as temperature chambers. It is grouped into families.',
        value={
            "url": "http://localhost:8001/equipment/a7bd4c43-29c7-40f1-bcf7-a2924ed474c2/",
            "id": "a7bd4c43-29c7-40f1-bcf7-a2924ed474c2",
            "identifier": "1234567890",
            "family": "http://localhost:8001/equipment_families/947e1f7c-c5b9-47b8-a121-d1e519a7154c/",
            "calibration_date": "2019-01-01",
            "in_use": True,
            "team": "http://localhost:8001/teams/1/",
            "cycler_tests": [
                "http://localhost:8001/cycler_tests/2b7313c9-94c2-4276-a4ee-e9d58d8a641b/"
            ],
            "permissions": {
                "create": True,
                "destroy": False,
                "write": True,
                "read": True
            }
        },
        response_only=True, # signal that example only applies to responses
    ),
])
class EquipmentSerializer(CustomPropertiesModelSerializer, PermissionsMixin, WithTeamMixin, ValidationPresentationMixin):
    family = TruncatedHyperlinkedRelatedIdField(
        'EquipmentFamilySerializer',
        ['type', 'manufacturer', 'model'],
        'equipmentfamily-detail',
        queryset=EquipmentFamily.objects.all(),
        help_text="Equipment Family this Equipment belongs to"
    )
    cycler_tests = TruncatedHyperlinkedRelatedIdField(
        'CyclerTestSerializer',
        ['cell', 'schedule'],
        'cyclertest-detail',
        read_only=True,
        many=True,
        help_text="Cycler Tests using this Equipment"
    )

    class Meta:
        model = Equipment
        fields = [
            'url', 'id', 'identifier', 'family', 'calibration_date', 'in_use', 'team', 'cycler_tests',
            'permissions', 'read_access_level', 'edit_access_level', 'delete_access_level', 'custom_properties'
        ]
        read_only_fields = ['url', 'id', 'datasets', 'in_use', 'cycler_tests', 'permissions']


@extend_schema_serializer(examples = [
    OpenApiExample(
        'Valid example',
        summary='Schedule Family details',
        description='Schedule Families group together properties shared by multiple Schedules.',
        value={
            "url": "http://localhost:8001/schedule_families/e25f7c94-ca32-4f47-b95a-3b0e7ae4a47f/",
            "id": "e25f7c94-ca32-4f47-b95a-3b0e7ae4a47f",
            "identifier": "Cell Conditioning",
            "description": "Each cell is cycled five times at 1C discharge and the standard charge. This test is completed at 25◦C.",
            "ambient_temperature_c": 25.0,
            "pybamm_template": [
                "Charge at 1 A until 4.1 V",
                "Discharge at {standard_discharge_constant_current} C for 10 hours or until 3.3 V",
                "Charge at 1 A until 4.1 V",
                "Discharge at {standard_discharge_constant_current} C for 10 hours or until 3.3 V",
                "Charge at 1 A until 4.1 V",
                "Discharge at C/1 for 10 hours or until 3.3 V",
                "Charge at {fast_charge_constant_current} until {fast_charge_constant_voltage} V",
                "Discharge at {standard_discharge_constant_current} C for 10 hours or until 3.3 V",
                "Charge at 1 A until 4.1 V",
                "Discharge at {standard_discharge_constant_current} C for 10 hours or until 3.3 V"
            ],
            "in_use": True,
            "team": "http://localhost:8001/teams/1/",
            "schedules": [
                "http://localhost:8001/schedules/5a2d7da9-393c-44ee-827a-5d15133c48d6/",
                "http://localhost:8001/schedules/7771fc54-7209-4564-9ec7-e87855f7ee67/"
            ],
            "permissions": {
                "create": True,
                "destroy": False,
                "write": True,
                "read": True
            }
        },
        response_only=True, # signal that example only applies to responses
    ),
])
class ScheduleFamilySerializer(CustomPropertiesModelSerializer, PermissionsMixin, WithTeamMixin):
    identifier = GetOrCreateTextField(foreign_model=ScheduleIdentifiers)
    schedules = TruncatedHyperlinkedRelatedIdField(
        'ScheduleSerializer',
        ['family'],
        'schedule-detail',
        read_only=True,
        many=True,
        help_text="Schedules belonging to this Schedule Family"
    )

    def validate_pybamm_template(self, value):
        # TODO: validate pybamm template against pybamm.step.string
        return value

    class Meta:
        model = ScheduleFamily
        fields = [
            'url', 'id', 'identifier', 'description',
            'ambient_temperature_c', 'pybamm_template',
            'in_use', 'team', 'schedules', 'permissions',
            'read_access_level', 'edit_access_level', 'delete_access_level',
            'custom_properties'
        ]
        read_only_fields = ['url', 'id', 'in_use', 'schedules', 'permissions']


@extend_schema_serializer(examples = [
    OpenApiExample(
        'Valid example',
        summary='Schedule details',
        description='Schedules are used to define the current profile used in a cycler test. They are grouped into families.',
        value={
            "url": "http://localhost:8001/schedules/5a2d7da9-393c-44ee-827a-5d15133c48d6/",
            "id": "5a2d7da9-393c-44ee-827a-5d15133c48d6",
            "family": "http://localhost:8001/schedule_families/e25f7c94-ca32-4f47-b95a-3b0e7ae4a47f/",
            "schedule_file": None,
            "pybamm_schedule_variables": {
                "fast_charge_constant_current": 1.0,
                "fast_charge_constant_voltage": 4.1,
                "standard_discharge_constant_current": 1.0
            },
            "in_use": True,
            "team": "http://localhost:8001/teams/1/",
            "cycler_tests": [
                "http://localhost:8001/cycler_tests/2b7313c9-94c2-4276-a4ee-e9d58d8a641b/"
            ],
            "permissions": {
                "create": True,
                "destroy": False,
                "write": True,
                "read": True
            }
        },
        response_only=True, # signal that example only applies to responses
    ),
])
class ScheduleSerializer(CustomPropertiesModelSerializer, PermissionsMixin, WithTeamMixin, ValidationPresentationMixin):
    family = TruncatedHyperlinkedRelatedIdField(
        'ScheduleFamilySerializer',
        ['identifier'],
        'schedulefamily-detail',
        queryset=ScheduleFamily.objects.all(),
        help_text="Schedule Family this Schedule belongs to"
    )
    cycler_tests = TruncatedHyperlinkedRelatedIdField(
        'CyclerTestSerializer',
        ['cell', 'equipment'],
        'cyclertest-detail',
        read_only=True,
        many=True,
        help_text="Cycler Tests using this Schedule"
    )

    def validate_pybamm_schedule_variables(self, value):
        template = self.instance.family.pybamm_template
        if template is None and value is not None:
            raise ValidationError("pybamm_schedule_variables has no effect if pybamm_template is not set")
        if value is None:
            return value
        keys = self.instance.family.pybamm_template_variable_names()
        for k, v in value.items():
            if k not in keys:
                raise ValidationError(f"Schedule variable {k} is not in the template")
            try:
                float(v)
            except (ValueError, TypeError):
                raise ValidationError(f"Schedule variable {k} must be a number")
        return value

    def validate(self, data):
        if data.get('schedule_file') is None:
            try:
                family = data.get('family') or self.instance.family
                assert family.pybamm_template is not None
            except (AttributeError, AssertionError):
                raise ValidationError("Schedule_file must be provided where pybamm_template is not set")
        return data

    class Meta:
        model = Schedule
        fields = [
            'url', 'id', 'family',
            'schedule_file', 'pybamm_schedule_variables',
            'in_use', 'team', 'cycler_tests', 'permissions',
            'read_access_level', 'edit_access_level', 'delete_access_level',
            'custom_properties'
        ]
        read_only_fields = ['url', 'id', 'in_use', 'cycler_tests', 'permissions']


@extend_schema_serializer(examples = [
    OpenApiExample(
        'Valid example',
        summary='Cycler Test details',
        description='Cycler Tests are the core of the system. They define the cell, equipment, and schedule used in a test, and are used to store the raw data produced by the test.',
        value={
            "url": "http://localhost:8001/cycler_tests/2b7313c9-94c2-4276-a4ee-e9d58d8a641b/",
            "id": "2b7313c9-94c2-4276-a4ee-e9d58d8a641b",
            "cell": "http://localhost:8001/cells/6a3a910b-d42e-46f6-9604-6fb3c2f3d059/",
            "equipment": [
                "http://localhost:8001/equipment/a7bd4c43-29c7-40f1-bcf7-a2924ed474c2/",
                "http://localhost:8001/equipment/12039516-72bf-42b7-a687-cb210ca4a087/"
            ],
            "schedule": "http://localhost:8001/schedules/5a2d7da9-393c-44ee-827a-5d15133c48d6/",
            "rendered_schedule": [
                "Charge at 1 A until 4.1 V",
                "Discharge at 1 C for 10 hours or until 3.3 V",
                "Charge at 1 A until 4.1 V",
                "Discharge at 1 C for 10 hours or until 3.3 V",
                "Charge at 1 A until 4.1 V",
                "Discharge at C/1 for 10 hours or until 3.3 V",
                "Charge at 1.0 until 4.1 V",
                "Discharge at 1 C for 10 hours or until 3.3 V",
                "Charge at 1 A until 4.1 V",
                "Discharge at 1 C for 10 hours or until 3.3 V"
            ],
            "team": "http://localhost:8001/teams/1/",
            "permissions": {
                "create": True,
                "destroy": False,
                "write": True,
                "read": True
            }
        },
        response_only=True, # signal that example only applies to responses
    ),
])
class CyclerTestSerializer(CustomPropertiesModelSerializer, PermissionsMixin, WithTeamMixin):
    rendered_schedule = serializers.SerializerMethodField(help_text="Rendered schedule")
    schedule = TruncatedHyperlinkedRelatedIdField(
        'ScheduleSerializer',
        ['family'],
        'schedule-detail',
        queryset=Schedule.objects.all(),
        help_text="Schedule this Cycler Test uses"
    )
    cell = TruncatedHyperlinkedRelatedIdField(
        'CellSerializer',
        ['identifier', 'family'],
        'cell-detail',
        queryset=Cell.objects.all(),
        help_text="Cell this Cycler Test uses"
    )
    equipment = TruncatedHyperlinkedRelatedIdField(
        'EquipmentSerializer',
        ['identifier', 'family'],
        'equipment-detail',
        queryset=Equipment.objects.all(),
        many=True,
        help_text="Equipment this Cycler Test uses"
    )
    files = TruncatedHyperlinkedRelatedIdField(
        'ObservedFileSerializer',
        ['name', 'path', 'parquet_partitions', 'png'],
        'observedfile-detail',
        queryset=ObservedFile.objects.all(),
        many=True,
        allow_null=True,
        allow_empty=True,
        help_text="Files harvested for this Cycler Test"
    )

    def get_rendered_schedule(self, instance) -> list[str] | None:
        if instance.schedule is None:
            return None
        return instance.rendered_pybamm_schedule(False)

    def validate(self, data):
        if data.get('schedule') is not None:
            try:
                render_pybamm_schedule(data['schedule'], data['cell'])
            except ScheduleRenderError as e:
                raise ValidationError(e)
        return data

    class Meta:
        model = CyclerTest
        fields = [
            'url', 'id', 'cell', 'equipment', 'files', 'schedule', 'rendered_schedule', 'team', 'permissions',
            'read_access_level', 'edit_access_level', 'delete_access_level',
            'custom_properties'
        ]
        read_only_fields = ['url', 'id', 'rendered_schedule', 'permissions']


@extend_schema_serializer(examples = [
    OpenApiExample(
        'Valid example',
        summary='Harvester details',
        description='Harvesters are the interface between the system and the raw data produced by cycler tests. They are responsible for uploading data to the system.',
        value={
            "url": "http://localhost:8001/harvesters/d8290e68-bfbb-3bc8-b621-5a9590aa29fd/",
            "id": "d8290e68-bfbb-3bc8-b621-5a9590aa29fd",
            "name": "Example Harvester",
            "sleep_time": 60,
            "environment_variables": {
                "EXAMPLE_ENV_VAR": "example value"
            },
            "active": True,
            "last_check_in": "2021-08-18T15:23:45.123456Z",
            "lab": "http://localhost:8001/labs/1/",
            "permissions": {
                "create": False,
                "destroy": False,
                "write": True,
                "read": True
            }
        },
        response_only=True, # signal that example only applies to responses
    ),
])
class HarvesterSerializer(serializers.HyperlinkedModelSerializer, PermissionsMixin):
    lab = TruncatedHyperlinkedRelatedIdField(
        'LabSerializer',
        ['name'],
        'lab-detail',
        read_only=True,
        help_text="Lab this Harvester belongs to"
    )

    class EnvField(serializers.DictField):
        # respresentation for json
        def to_representation(self, value) -> dict[str, str]:
            view = self.context.get('view')
            if view and view.action == 'list':
                return {}
            return {v.key: v.value for v in value.all() if not v.deleted}

        # representation for python object
        def to_internal_value(self, values):
            for k in values.keys():
                if not re.match(r'^[a-zA-Z0-9_]+$', k):
                    raise ValidationError(f'Key {k} is not alpha_numeric')
            for k, v in values.items():
                k = k.upper()
                try:
                    env = HarvesterEnvVar.objects.get(harvester=self.root.instance, key=k)
                    env.value = v
                    env.deleted = False
                    env.save()
                except HarvesterEnvVar.DoesNotExist:
                    HarvesterEnvVar.objects.create(harvester=self.root.instance, key=k, value=v)
            envvars = HarvesterEnvVar.objects.filter(harvester=self.root.instance, deleted=False)
            input_keys = [k.upper() for k in values.keys()]
            for v in envvars.all():
                if v.key not in input_keys:
                    v.deleted = True
                    v.save()
            return HarvesterEnvVar.objects.filter(harvester=self.root.instance, deleted=False)

    environment_variables = EnvField(help_text="Environment variables set on this Harvester")

    def validate_name(self, value):
        harvesters = Harvester.objects.filter(name=value)
        if self.instance is not None:
            harvesters = harvesters.exclude(id=self.instance.id)
            harvesters = harvesters.filter(lab=self.instance.lab)
        if harvesters.exists():
            raise ValidationError('Harvester with that name already exists')
        return value

    def validate_sleep_time(self, value):
        try:
            value = int(value)
            assert value > 0
            return value
        except (TypeError, ValueError, AssertionError):
            return ValidationError('sleep_time must be an integer greater than 0')

    class Meta:
        model = Harvester
        read_only_fields = ['url', 'id', 'last_check_in', 'last_check_in_job', 'lab', 'permissions']
        fields = [*read_only_fields, 'name', 'sleep_time', 'environment_variables', 'active']
        extra_kwargs = augment_extra_kwargs()

@extend_schema_serializer(examples = [
    OpenApiExample(
        'Valid example',
        summary='Monitored Path details',
        description='Monitored Paths are subdirectories on Harvesters that are monitored for new files. When a new file is detected, it is uploaded to the system.',
        value={
            "url": "http://localhost:8001/monitored_paths/172f2460-9528-11ee-8454-eb9d381d3cc4/",
            "id": "172f2460-9528-11ee-8454-eb9d381d3cc4",
            "files": ["http://localhost:8001/files/c690ddf0-9527-11ee-8454-eb9d381d3cc4/"],
            "path": "/home/example_user/example_data.csv",
            "regex": ".*\\.csv",
            "stable_time_s": 60,
            "active": True,
            "harvester": "http://localhost:8001/harvesters/d8290e68-bfbb-3bc8-b621-5a9590aa29fd/",
            "team": "http://localhost:8001/teams/1/",
            "permissions": {
                "create": False,
                "destroy": False,
                "write": True,
                "read": True
            }
        },
        response_only=True, # signal that example only applies to responses
    ),
])
class MonitoredPathSerializer(serializers.HyperlinkedModelSerializer, PermissionsMixin, WithTeamMixin, CreateOnlyMixin):
    files = serializers.SerializerMethodField(help_text="Files on this MonitoredPath")
    edit_access_level = serializers.ChoiceField(
        choices=[(v.value, v.label) for v in ALLOWED_USER_LEVELS_EDIT_PATH],
        help_text="Minimum user level required to edit this resource",
        allow_null=True,
        required=False
    )

    def get_files(self, instance) -> list[OpenApiTypes.URI]:
        """Return only URLs because otherwise it takes _forever_."""
        files = ObservedFile.objects.filter(harvester=instance.harvester).values("path", "id")
        file_urls = []
        for file in files:
            if instance.matches(file.get('path')):
                file_urls.append(reverse('observedfile-detail', (file.get('id'),)))
        return file_urls

    harvester = TruncatedHyperlinkedRelatedIdField(
        'HarvesterSerializer',
        ['name'],
        'harvester-detail',
        queryset=Harvester.objects.all(),
        help_text="Harvester this MonitoredPath is on",
        create_only=True
    )

    team = TruncatedHyperlinkedRelatedIdField(
        'TeamSerializer',
        ['name'],
        'team-detail',
        queryset=Team.objects.all(),
        help_text="Team this MonitoredPath belongs to",
        create_only=True
    )

    def validate_harvester(self, value):
        if self.instance is not None:
            return self.instance.harvester  # harvester cannot be changed
        request = self.context['request']
        if value.lab.pk not in get_user_auth_details(request).lab_ids:
            raise ValidationError("You may only create MonitoredPaths on Harvesters in your own lab(s)")
        return value

    def validate_team(self, value):
        """
        Only team admins can create monitored paths.
        Monitored paths can read arbitrary files on the harvester system,
        so some level of trust is required to allow users to create them.
        """
        if self.instance is not None:
            return self.instance.team
        if value.pk not in self.context['request'].user_auth_details.writeable_team_ids:
            raise ValidationError("You may only create MonitoredPaths in your own team(s)", code=HTTP_403_FORBIDDEN)
        return value

    def validate_path(self, value):
        try:
            value = str(value).lower().lstrip().rstrip()
        except BaseException as e:
            raise ValidationError(f"Invalid path: {e.__context__}")
        abs_path = os.path.normpath(value)
        return abs_path

    def validate_stable_time_s(self, value):
        try:
            v = int(value)
            assert v > 0
            return v
        except (TypeError, ValueError, AssertionError):
            raise ValidationError(f"stable_time_s value '{value}' is not a positive integer")

    def validate_regex(self, value):
        try:
            re.compile(value)
            return value
        except BaseException as e:
            raise ValidationError(f"Invalid regex: {e.__context__}")

    class Meta:
        model = MonitoredPath
        fields = [
            'url', 'id', 'path', 'regex', 'stable_time_s', 'active', 'max_partition_line_count',
            'files', 'harvester', 'team',
            'permissions', 'read_access_level', 'edit_access_level', 'delete_access_level'
        ]
        read_only_fields = ['url', 'id', 'files', 'harvester', 'permissions']
        extra_kwargs = augment_extra_kwargs({
            'harvester': {'create_only': True},
            'team': {'create_only': True}
        })


class ColumnMappingSerializer(serializers.HyperlinkedModelSerializer, WithTeamMixin, PermissionsMixin):
    rendered_map = serializers.SerializerMethodField()

    def get_rendered_map(self, instance) -> dict:
        out = {}
        for key, value in instance.map.items():
            data_column_type = DataColumnType.objects.get(pk=value['column_type'])
            out[key] = {
                'new_name': value.get('new_name', data_column_type.name),
                'data_type': data_column_type.data_type
            }
            if data_column_type.data_type in ['int', 'float']:
                out[key]['multiplier'] = value.get('multiplier', 1)
                out[key]['addition'] = value.get('addition', 0)
        return out

    def validate_map(self, value):
        """
        Expect a dictionary with the structure:
        ```
        {
            "column_name_in_file": {
                "column_type": "DataColumnType uuid",
                "new_name": "str",
                "multiplier": "float",
                "addition": "float"
            },
            ...
        }
        ```
        `new_name` is optional and defaults to the name of the DataColumnType.
        `addition` and `multiplier` are optional.
        If they are not provided, they default to 0 and 1 respectively.
        They are only used for numerical columns.
        New column values = (old column value + addition) * multiplier.
        E.g. to convert from degrees C to K, you would set multiplier to 1 and addition to 273.15.
        """
        if not isinstance(value, dict):
            raise ValidationError("Map must be a dictionary")
        required_column_ids = [c.pk for c in DataColumnType.objects.filter(is_required=True)]
        required_columns_supplied = {}
        new_value = {}
        for k, v in value.items():
            new_value[k] = v
            if not isinstance(k, str):
                raise ValidationError("Keys must be strings representing the names of columns in the file")
            if not isinstance(v, dict):
                raise ValidationError("Values must be dictionaries")
            try:
                column_type = DataColumnType.objects.get(pk=v.get('column_type'))
            except DataColumnType.DoesNotExist:
                if v.get('column_type') is None:
                    raise ValidationError(
                        f"No column_type specified for column '{k}' - perhaps you should use Unknown"
                    )
                raise ValidationError(f"Invalid column_type id '{v.get('column_type')}' for column '{k}'")
            if column_type.pk in required_column_ids:
                if column_type.pk in required_columns_supplied.keys():
                    raise ValidationError(
                        f"Cannot assign column '{k}' to required column {column_type.name}. "
                        f"Column {column_type.name} is already assigned to column '{required_columns_supplied[column_type.pk]}'"
                    )
                required_columns_supplied[column_type.pk] = k
            if 'new_name' in v:
                if column_type.is_required:
                    raise ValidationError(f"Column '{k}' is one of the core required columns and cannot be renamed")
                if not isinstance(v['new_name'], str):
                    raise ValidationError(f"new_name for column '{k}' must be a string")
            if column_type.data_type in ['int', 'float']:
                type_fn = int if column_type.data_type == 'int' else float
                try:
                    new_value[k]['multiplier'] = type_fn(v.get('multiplier', 1))
                    new_value[k]['addition'] = type_fn(v.get('addition', 0))
                except (ValueError, TypeError) as e:
                    raise ValidationError(
                        f"Multiplier and addition for column '{k}' must be {column_type.data_type}"
                    ) from e
            elif 'multiplier' in v:
                raise ValidationError(f"Column '{k}' is not numerical, so it cannot have a multiplier")
            elif 'addition' in v:
                raise ValidationError(f"Column '{k}' is not numerical, so it cannot have an addition")
        return new_value

    def validate(self, attrs):
        if (self.instance and
                self.instance.map != attrs['map'] and
                self.instance.in_use and
                not all([
                    f.has_object_write_permission(request=self.context['request'])
                    for f in self.instance.observed_files.all()
                ])
        ):
            raise ValidationError("You cannot modify a mapping that is in use by files you cannot write to.")
        return super().validate(attrs)

    class Meta:
        model = ColumnMapping
        fields = [
            'url', 'id',
            'name', 'map',
            'rendered_map', 'is_valid', 'missing_required_columns', 'in_use',
            'team', 'permissions', 'read_access_level', 'edit_access_level', 'delete_access_level'
        ]
        read_only_fields = ['url', 'id', 'rendered_map', 'is_valid', 'missing_required_columns', 'permissions']


class ParquetPartitionSerializer(serializers.HyperlinkedModelSerializer, PermissionsMixin):
    observed_file = TruncatedHyperlinkedRelatedIdField(
        'ObservedFileSerializer',
        ['name', 'state', 'parser', 'num_rows'],
        'observedfile-detail',
        read_only=True,
        help_text="Observed File this Parquet Partition belongs to"
    )

    class Meta:
        model = ParquetPartition
        read_only_fields = [
            'url', 'id',
            'parquet_file', 'observed_file',
            'partition_number', 'uploaded', 'upload_errors',
            'permissions'
        ]
        fields = read_only_fields


@extend_schema_serializer(examples = [
    OpenApiExample(
        'Valid example',
        summary='Observed File details',
        description='Observed Files are the raw data produced by cycler tests. They are uploaded to the system by Harvesters.',
        value={
            "url": "http://localhost:8001/files/19b16096-737f-4d94-8cc6-802dbf129704/",
            "id": "19b16096-737f-4d94-8cc6-802dbf129704",
            "harvester": {
                "id": "340efe2d-1040-4992-ae38-87d7e06b8054",
                "name": "Harvey",
                "url": "http://localhost:8001/harvesters/340efe2d-1040-4992-ae38-87d7e06b8054/"
            },
            "name": None,
            "path": "/usr/harvester/.test-data/test-suite-small/headerless.csv",
            "state": "IMPORTED",
            "parser": "DelimitedInputFile",
            "num_rows": 4,
            "first_sample_no": None,
            "last_sample_no": None,
            "extra_metadata": {
                "column_0": {
                    "has_data": True
                },
                "column_1": {
                    "has_data": True
                },
                "column_2": {
                    "has_data": True
                },
                "column_3": {
                    "has_data": True
                },
                "column_4": {
                    "has_data": True
                }
            },
            "has_required_columns": False,
            "last_observed_time": "2024-04-10T14:35:44.467420Z",
            "last_observed_size_bytes": 225,
            "column_errors": [
                "Missing required column: Elapsed_time_s",
                "Missing required column: Voltage_V",
                "Missing required column: Current_A"
            ],
            "upload_errors": [],
            "parquet_partitions": [
                {
                    "upload_errors": [],
                    "url": "http://localhost:8001/parquet_partitions/359e1a24-6d7f-4aaa-adc2-2a9d8a8c7c48/",
                    "partition_number": 0,
                    "id": "359e1a24-6d7f-4aaa-adc2-2a9d8a8c7c48",
                    "parquet_file": "http://localhost:8001/parquet_partitions/359e1a24-6d7f-4aaa-adc2-2a9d8a8c7c48/file/",
                    "uploaded": True
                }
            ],
            "columns": [
                {
                    "name": "column_0",
                    "url": "http://localhost:8001/columns/30/",
                    "name_in_file": "column_0",
                    "id": 30,
                    "type": None
                },
                {
                    "name": "column_1",
                    "url": "http://localhost:8001/columns/31/",
                    "name_in_file": "column_1",
                    "id": 31,
                    "type": None
                },
                {
                    "name": "column_2",
                    "url": "http://localhost:8001/columns/32/",
                    "name_in_file": "column_2",
                    "id": 32,
                    "type": None
                },
                {
                    "name": "column_3",
                    "url": "http://localhost:8001/columns/33/",
                    "name_in_file": "column_3",
                    "id": 33,
                    "type": None
                },
                {
                    "name": "column_4",
                    "url": "http://localhost:8001/columns/34/",
                    "name_in_file": "column_4",
                    "id": 34,
                    "type": None
                }
            ],
            "permissions": {
                "write": True,
                "read": True
            }
        },
        response_only=True, # signal that example only applies to responses
    ),
])
class ObservedFileSerializer(serializers.HyperlinkedModelSerializer, PermissionsMixin):
    parquet_partitions = TruncatedHyperlinkedRelatedIdField(
        'ParquetPartitionSerializer',
        ['parquet_file', 'partition_number', 'uploaded', 'upload_errors'],
        'parquetpartition-detail',
        read_only=True,
        many=True,
        help_text="Parquet partitions of this File"
    )
    mapping = TruncatedHyperlinkedRelatedIdField(
        'ColumnMappingSerializer',
        ['name', 'is_valid'],
        'columnmapping-detail',
        queryset=ColumnMapping.objects.all(),
        help_text="ColumnMapping applied to this File"
    )
    harvester = TruncatedHyperlinkedRelatedIdField(
        'HarvesterSerializer',
        ['name'],
        'harvester-detail',
        read_only=True,
        help_text="Harvester this File belongs to"
    )
    applicable_mappings = serializers.SerializerMethodField(
        help_text="Mappings that can be applied to this File"
    )
    extra_metadata = serializers.SerializerMethodField(
        help_text="Extra metadata about this File"
    )
    summary = serializers.SerializerMethodField(
        help_text="First few rows of this file's data"
    )

    def get_applicable_mappings(self, instance) -> str:
        return reverse(
            'observedfile-applicable-mappings',
            args=[instance.pk],
            request=self.context.get('request')
        )

    def get_extra_metadata(self, instance) -> Union[dict, None]:
        return reverse(
            'observedfile-extra-metadata',
            args=[instance.pk],
            request=self.context.get('request')
        )

    def get_summary(self, instance) -> Union[dict, None]:
        return reverse(
            'observedfile-summary',
            args=[instance.pk],
            request=self.context.get('request')
        )

    class Meta:
        model = ObservedFile
        fields = [
            'url', 'id', 'name',
            'path', 'harvester', 'uploader',
            'state',
            'parser',
            'upload_errors',
            'num_rows',
            'first_sample_no',
            'last_sample_no',
            'last_observed_time', 'last_observed_size_bytes',
            'mapping',
            'has_required_columns',
            'parquet_partitions',
            'extra_metadata',
            'summary',
            'png',
            'applicable_mappings',
            'permissions'
        ]
        read_only_fields = list(set(fields) - {'name', 'mapping'})
        extra_kwargs = augment_extra_kwargs({
            'upload_errors': {'help_text': "Errors associated with this File"}
        })


class ObservedFileCreateSerializer(ObservedFileSerializer, WithTeamMixin):
    """
    The ObservedFileCreateSerializer is used to create ObservedFiles from uploaded files.
    This can be done in a one- or two-step process.

    In the one-step process, a file is supplied along with a `mapping` that is applicable to that file.
    An ObservedFile is created with the supplied mapping applied to the file.
    Its data and preview image are extracted and stored in Storage.

    In the two-stage process, a file is supplied without a `mapping`.
    The `summary` of the file is extracted and stored in a new ObservedFile.
    The user can then apply a mapping to that file in the usual manner.
    Once a mapping has been applied, the file can be re-uploaded citing its `id` to complete the process.
    The data and preview image will be extracted and stored in Storage.
    """
    file = serializers.FileField(write_only=True, help_text="File to upload")
    target_file_id = TruncatedHyperlinkedRelatedIdField(
        'ObservedFileSerializer',
        ['name', 'path', 'parquet_partitions', 'png'],
        'observedfile-detail',
        queryset=ObservedFile.objects.all(),
        help_text="ID of the ObservedFile to complete creation of",
        required=False
    )
    uploader = TruncatedHyperlinkedRelatedIdField(
        'UserSerializer',
        ['username', 'first_name', 'last_name'],
        'userproxy-detail',
        queryset=UserProxy.objects.all(),
        many=False,
        help_text="Users uploading the data"
    )
    mapping = TruncatedHyperlinkedRelatedIdField(
        'ColumnMappingSerializer',
        ['name'],
        'columnmapping-detail',
        queryset=ColumnMapping.objects.all(),
        help_text="ColumnMapping applied to this File",
        required=False
    )
    team = TruncatedHyperlinkedRelatedIdField(
        'TeamSerializer',
        ['name'],
        'team-detail',
        queryset=Team.objects.all(),
        help_text="Team this File belongs to"
    )

    def validate_uploader(self, value):
        user = self.context['request'].user
        if not self.instance and not value:
            raise ValidationError("You must provide the `uploader` user id")
        if self.instance is not None and self.instance.uploader != user and not user.is_superuser:
            raise ValidationError("You may not change the uploader of a File")
        if value != user and not value.is_superuser:
            raise ValidationError("You may only create Files for yourself")
        if len(get_user_auth_details(self.context['request']).team_ids) == 0:
            raise ValidationError("You must be a member of a team to create Files")
        return value

    def validate_id(self, value):
        if value is None:
            return None
        try:
            file = ObservedFile.objects.get(pk=value)
        except ObservedFile.DoesNotExist:
            file = None
        if file is None or file.state != FileState.AWAITING_MAP_ASSIGNMENT:
            raise ValidationError("File must be in AWAITING_MAP_ASSIGNMENT state")
        if file and file.uploader is self.context['request'].user:
            raise ValidationError("You may only update your files.")
        return value

    def validate(self, attrs):
        if 'file' not in attrs:
            raise ValidationError("You must provide a file to upload")
        if 'id' in attrs and 'mapping' not in attrs:
            if self.instance is None or self.instance.mapping is None:
                raise ValidationError("You must specify the mapping when updating an existing file")
        return super().validate(attrs)

    def to_representation(self, instance):
        return super().to_representation(instance)

    def save(self, **kwargs):
        return super().save(**kwargs)

    def create(self, validated_data):
        """
        Create an ObservedFile from a file upload.
        This happens in two steps:
        1. Save the uploaded file to a temporary location
        2. Attempt to process the file using the galv-harvester package
        3. If successful, create the ObservedFile

        We'll go through this process twice: once to summarise the columns and once to apply the mapping.
        Even though we have to upload data twice,
        it's less of a headache than trying to create persistent temporary storage somewhere
        and police the limits on it.
        """
        target_file = validated_data.pop('target_file_id', None)
        file = validated_data.pop('file')
        mapping = validated_data.pop('mapping', None)
        observed_file = None
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            for chunk in file.chunks():
                temp_file.write(chunk)
        try:
            harvester = InternalHarvestProcessor(temp_file.name)
            summary = harvester.summarise_columns()
            if target_file is not None:
                observed_file = target_file
                if observed_file.summary != json.loads(summary.to_json()):  # apply the same processing to summary
                    raise ValidationError("Summary does not match existing file")
                if mapping is None:
                    mapping = observed_file.mapping
            else:
                observed_file = ObservedFile.objects.create(
                    **validated_data,
                    summary=summary.to_dict(),
                )
            if mapping is not None:
                if mapping not in [m['mapping'] for m in observed_file.applicable_mappings(self.context['request'])]:
                    raise ValidationError("Mapping is not applicable to this file")
                observed_file.mapping = mapping
                observed_file.save()
                # Apply mapping and save the file contents
                harvester.mapping = ColumnMappingSerializer(
                    mapping,
                    context=self.context
                ).data.get('rendered_map')
                harvester.process_data()
                # Save parquet partitions to storage
                dir, _, partitions = list(os.walk(harvester.data_file_name))[0]
                for i, name in enumerate([os.path.join(dir, p) for p in partitions]):
                    if name.endswith('.parquet'):
                        ParquetPartition.objects.create(
                            observed_file=observed_file,
                            partition_number=i,
                            bytes_required=Path(name).stat().st_size,
                            parquet_file=File(file=open(name, 'rb'), name=os.path.basename(name))
                        )

                observed_file.png = File(
                    file=open(harvester.png_file_name, 'rb'),
                    name=os.path.basename(harvester.png_file_name)
                )
                observed_file.state = FileState.IMPORTED
            else:
                observed_file.state = FileState.AWAITING_MAP_ASSIGNMENT
            observed_file.save()
            return observed_file
        except Exception as e:
            if observed_file:
                observed_file.state = FileState.IMPORT_FAILED
                observed_file.save()
            raise ValidationError(f"Error processing file: {e}")
        finally:
            os.unlink(temp_file.name)

    def update(self, instance, validated_data):
        """
        The update method is only ever used to do the second half of the double-upload.
        The first half will have created the ObservedFile instance and its summary,
        and now the user will have selected a mapping for it.

        Consequently, we simply repeat the create step, knowing that we now have all the required information.
        The create step will even validate that this looks like the same file by checking the summary
        against the existing ObservedFile instance's summary.
        """
        return self.create(validated_data)


    class Meta(ObservedFileSerializer.Meta):
        fields = [
            "id", 'target_file_id', "path", "name", "uploader", "file", "mapping", "team", "state",
            "read_access_level", "edit_access_level", "delete_access_level",
        ]
        read_only_fields = []
        extra_kwargs = {
            "uploader": {"required": False, "allow_null": True},
            "team": {"required": True, "allow_null": False},
        }


@extend_schema_serializer(examples = [
    OpenApiExample(
        'Valid example',
        summary='Harvest Error details',
        description='Harvest Errors are errors encountered by Harvesters when uploading data to the system.',
        value={
            "url": "http://localhost:8001/harvest_errors/1/",
            "id": 1,
            "harvester": "http://localhost:8001/harvesters/d8290e68-bfbb-3bc8-b621-5a9590aa29fd/",
            "file": "http://localhost:8001/observed_files/1/",
            "error": "Error message",
            "timestamp": "2021-08-18T15:23:45.123456Z",
            "permissions": {
                "create": False,
                "destroy": False,
                "write": False,
                "read": True
            }
        },
        response_only=True, # signal that example only applies to responses
    ),
])
class HarvestErrorSerializer(serializers.HyperlinkedModelSerializer, PermissionsMixin):
    harvester = TruncatedHyperlinkedRelatedIdField(
        'HarvesterSerializer',
        ['name', 'lab'],
        'harvester-detail',
        read_only=True,
        help_text="Harvester this HarvestError belongs to"
    )
    file = TruncatedHyperlinkedRelatedIdField(
        'ObservedFileSerializer',
        ['path'],
        'observedfile-detail',
        read_only=True,
        help_text="File this HarvestError belongs to"
    )

    class Meta:
        model = HarvestError
        fields = ['url', 'id', 'harvester', 'file', 'error', 'timestamp', 'permissions']
        extra_kwargs = augment_extra_kwargs()


class DataUnitSerializer(serializers.ModelSerializer, WithTeamMixin, PermissionsMixin):
    class Meta:
        model = DataUnit
        fields = ['url', 'id', 'name', 'symbol', 'description', 'is_default', 'team',
                  'permissions', 'read_access_level', 'edit_access_level', 'delete_access_level']
        read_only_fields = ['url', 'id', 'is_default', 'permissions']
        extra_kwargs = augment_extra_kwargs()


class DataColumnTypeSerializer(serializers.HyperlinkedModelSerializer, WithTeamMixin, PermissionsMixin):
    unit = TruncatedHyperlinkedRelatedIdField(
        'DataUnitSerializer',
        ['name', 'symbol'],
        view_name='dataunit-detail',
        queryset=DataUnit.objects.all(),
    )

    class Meta:
        model = DataColumnType
        fields = [
            'url', 'id',
            'name', 'description', 'is_default', 'is_required', 'unit', 'data_type',
            'team', 'permissions', 'read_access_level', 'edit_access_level', 'delete_access_level'
        ]
        read_only_fields = ['url', 'id', 'is_default', 'is_required', 'columns', 'permissions']
        extra_kwargs = augment_extra_kwargs()


@extend_schema_serializer(examples = [
    OpenApiExample(
        'Valid example',
        summary='Experiment details',
        description='Experiments are the highest level of abstraction in the system. They are used to group cycler tests and define the protocol used in those tests.',
        value={
            "url": "http://localhost:8001/experiments/1/",
            "id": "d8290e68-bfbb-3bc8-b621-5a9590aa29fd",
            "title": "Example Experiment",
            "description": "Example description",
            "authors": [
                "http://localhost:8001/userproxies/1/"
            ],
            "protocol": {
                "detail": "JSON representation of experiment protocol"
            },
            "protocol_file": None,
            "cycler_tests": [
                "http://localhost:8001/cycler_tests/2b7313c9-94c2-4276-a4ee-e9d58d8a641b/"
            ],
            "team": "http://localhost:8001/teams/1/",
            "permissions": {
                "create": True,
                "destroy": True,
                "write": True,
                "read": True
            }
        },
        response_only=True, # signal that example only applies to responses
    ),
])
class ExperimentSerializer(serializers.HyperlinkedModelSerializer, PermissionsMixin, WithTeamMixin):
    cycler_tests = TruncatedHyperlinkedRelatedIdField(
        'CyclerTestSerializer',
        ['cell', 'equipment', 'schedule'],
        'cyclertest-detail',
        queryset=CyclerTest.objects.all(),
        many=True,
        help_text="Cycler Tests using this Experiment"
    )
    authors = TruncatedHyperlinkedRelatedIdField(
        'UserSerializer',
        ['username', 'first_name', 'last_name'],
        'userproxy-detail',
        queryset=UserProxy.objects.all(),
        many=True,
        help_text="Users who created this Experiment"
    )

    class Meta:
        model = Experiment
        fields = [
            'url',
            'id',
            'title',
            'description',
            'authors',
            'protocol',
            'protocol_file',
            'cycler_tests',
            'team',
            'permissions'
        ]
        read_only_fields = ['url', 'id', 'permissions']
        extra_kwargs = augment_extra_kwargs()


@extend_schema_serializer(examples = [
    OpenApiExample(
        'Valid example',
        summary='Validation Schema details',
        description='Validation Schemas are used to define the expected format of data.',
        value={
            "url": "http://localhost:8001/validation_schemas/1/",
            "id": "df383510-9527-11ee-8454-eb9d381d3cc4",
            "name": "Example Validation Schema",
            "schema": {
                "type": "object",
                "properties": {
                    "example_property": {
                        "type": "string"
                    }
                },
                "required": [
                    "example_property"
                ]
            },
            "team": "http://localhost:8001/teams/1/",
            "permissions": {
                "create": True,
                "destroy": True,
                "write": True,
                "read": True
            }
        },
        response_only=True, # signal that example only applies to responses
    ),
])
class ValidationSchemaSerializer(serializers.HyperlinkedModelSerializer, PermissionsMixin, WithTeamMixin):
    def validate_schema(self, value):
        try:
            jsonschema.validate({}, value)
        except jsonschema.exceptions.SchemaError as e:
            raise ValidationError(e)
        except jsonschema.exceptions.ValidationError:
            pass
        return value

    class Meta:
        model = ValidationSchema
        fields = [
            'url', 'id', 'team', 'name', 'schema',
            'permissions', 'read_access_level', 'edit_access_level', 'delete_access_level'
        ]

@extend_schema_serializer(examples = [
    OpenApiExample(
        'Valid example',
        summary='Knox Token details',
        description='Knox Tokens are used to authenticate users with the system.',
        value={
            "url": "http://localhost:8001/tokens/1/",
            "id": 1,
            "name": "Example Token",
            "created": "2021-08-18T15:23:45.123456Z",
            "expiry": "2023-08-18T15:23:45.123456Z",
            "permissions": {
                "create": False,
                "destroy": False,
                "write": True,
                "read": True
            }
        },
        response_only=True, # signal that example only applies to responses
    ),
])
class KnoxTokenSerializer(serializers.HyperlinkedModelSerializer, PermissionsMixin):
    created = serializers.SerializerMethodField(help_text="Date and time of creation")
    expiry = serializers.SerializerMethodField(help_text="Date and time token expires (blank = never)")
    url = serializers.SerializerMethodField(help_text=url_help_text)

    def knox_token(self, instance):
        if not instance.user == self.context['request'].user:
            raise ValueError('Bad user ID for token access')
        return AuthToken.objects.get(user=instance.user, token_key=instance.knox_token_key)

    def get_created(self, instance) -> timezone.datetime:
        return self.knox_token(instance).created

    def get_expiry(self, instance) -> timezone.datetime | None:
        return self.knox_token(instance).expiry

    def get_url(self, instance) -> str:
        return reverse('tokens-detail', args=(instance.id,), request=self.context['request'])

    class Meta:
        model = KnoxAuthToken
        fields = ['url', 'id', 'name', 'created', 'expiry', 'permissions']
        read_only_fields = fields
        extra_kwargs = augment_extra_kwargs()


@extend_schema_serializer(examples = [
    OpenApiExample(
        'Valid example',
        summary='Knox Token Full details',
        description='Knox Tokens are used to authenticate users with the system. This serializer includes the token value.',
        value={
            "url": "http://localhost:8001/tokens/1/",
            "id": 1,
            "name": "Example Token",
            "token": "example_token_value",
            "created": "2021-08-18T15:23:45.123456Z",
            "expiry": "2023-08-18T15:23:45.123456Z",
            "permissions": {
                "create": False,
                "destroy": False,
                "write": True,
                "read": True
            }
        },
        response_only=True, # signal that example only applies to responses
    ),
])
class KnoxTokenFullSerializer(KnoxTokenSerializer):
    token = serializers.SerializerMethodField(help_text="Token value")

    def get_token(self, instance) -> str:
        return self.context['token']

    class Meta:
        model = KnoxAuthToken
        fields = ['url', 'id', 'name', 'created', 'expiry', 'token', 'permissions']
        read_only_fields = fields
        extra_kwargs = augment_extra_kwargs()

@extend_schema_serializer(examples = [
    OpenApiExample(
        'Valid example',
        summary='Harvester Configuration details',
        description='When Harvesters contact the system, they are given a configuration containing information about the system and the Harvester.',
        value={
            "url": "http://localhost:8001/harvesters/d8290e68-bfbb-3bc8-b621-5a9590aa29fd/",
            "id": "d8290e68-bfbb-3bc8-b621-5a9590aa29fd",
            "api_key": "example_api_key",
            "name": "Example Harvester",
            "sleep_time": 60,
            "monitored_paths": [
                "http://localhost:8001/monitored_paths/172f2460-9528-11ee-8454-eb9d381d3cc4/"
            ],
            "max_upload_bytes": 26214400,
            "environment_variables": {
                "EXAMPLE_ENV_VAR": "example value"
            },
            "deleted_environment_variables": [],
            "permissions": {
                "create": False,
                "destroy": False,
                "write": False,
                "read": True
            }
        },
        response_only=True, # signal that example only applies to responses
    ),
])
class HarvesterConfigSerializer(HarvesterSerializer, PermissionsMixin):
    max_upload_bytes = serializers.SerializerMethodField(help_text="Maximum upload size (bytes)")
    deleted_environment_variables = serializers.SerializerMethodField(help_text="Envvars to unset")
    monitored_paths = MonitoredPathSerializer(many=True, read_only=True, help_text="Directories to harvest")

    def get_max_upload_bytes(self, _):
        return DATA_UPLOAD_MAX_MEMORY_SIZE

    def get_deleted_environment_variables(self, instance):
        return [v.key for v in instance.environment_variables.all() if v.deleted]

    class Meta:
        model = Harvester
        fields = [
            'url', 'id', 'api_key', 'name', 'sleep_time', 'monitored_paths',
            'max_upload_bytes', 'environment_variables', 'deleted_environment_variables', 'permissions'
        ]
        read_only_fields = fields
        extra_kwargs = augment_extra_kwargs({
            'environment_variables': {'help_text': "Envvars set on this Harvester"}
        })
        depth = 1


class HarvesterCreateSerializer(HarvesterSerializer, PermissionsMixin):
    lab = TruncatedHyperlinkedRelatedIdField(
        'LabSerializer',
        ['name'],
        'lab-detail',
        queryset=Lab.objects.all(),
        required=True,
        help_text="Lab this Harvester belongs to"
    )

    def validate_lab(self, value):
        try:
            if value.pk in self.context['request'].user_auth_details.writeable_lab_ids:
                return value
        except:
            pass
        raise ValidationError("You may only create Harvesters in your own lab(s)")

    def to_representation(self, instance):
        return HarvesterConfigSerializer(context=self.context).to_representation(instance)

    class Meta:
        model = Harvester
        fields = ['name', 'lab', 'permissions']
        read_only_fields = ['permissions']
        extra_kwargs = {'name': {'required': True}, 'lab': {'required': True}}


class SchemaValidationSerializer(serializers.HyperlinkedModelSerializer, PermissionsMixin):
    schema = TruncatedHyperlinkedRelatedIdField(
        'ValidationSchemaSerializer',
        ['name'],
        'validationschema-detail',
        help_text="Validation schema used",
        read_only=True
    )

    validation_target = serializers.SerializerMethodField(help_text="Target of validation")

    @extend_schema_field(OpenApiTypes.URI)
    def get_validation_target(self, instance):
        return reverse(
            f"{instance.content_type.model}-detail",
            args=(instance.object_id,),
            request=self.context['request']
        )

    class Meta:
        model = SchemaValidation
        fields = ['url', 'id', 'schema', 'validation_target', 'status', 'permissions', 'detail', 'last_update']
        read_only_fields = [*fields]
        extra_kwargs = augment_extra_kwargs()


class ArbitraryFileSerializer(serializers.HyperlinkedModelSerializer, PermissionsMixin, WithTeamMixin):

    class Meta:
        model = ArbitraryFile
        fields = [
            'url', 'id', 'name', 'description', 'file', 'team',
            'read_access_level', 'edit_access_level', 'delete_access_level', 'permissions'
        ]
        read_only_fields = ['url', 'id', 'file', 'permissions']
        extra_kwargs = augment_extra_kwargs()


class ArbitraryFileCreateSerializer(ArbitraryFileSerializer):
    class Meta(ArbitraryFileSerializer.Meta):
        read_only_fields = ['url', 'id', 'permissions']
        extra_kwargs = augment_extra_kwargs({'file': {'required': True}})

    def create(self, validated_data):
        file = validated_data.pop('file', None)
        bytes_required = file.size if file else 0
        with transaction.atomic():
            try:
                arbitrary_file = ArbitraryFile.objects.create(**validated_data, bytes_required=bytes_required)
                if file:
                    arbitrary_file.bytes_required = file.size
                    arbitrary_file.save()
                    arbitrary_file.file.save(file.name, file, save=True)
            except StorageError as e:
                raise ValidationError(e)
        return arbitrary_file
