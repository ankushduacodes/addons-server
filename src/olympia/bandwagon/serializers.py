from django.core.serializers import serialize as object_serialize
from django.utils.translation import ugettext, ugettext_lazy as _

import six
import waffle

from rest_framework import serializers
from rest_framework.validators import UniqueTogetherValidator

from olympia.accounts.serializers import BaseUserSerializer
from olympia.addons.models import Addon
from olympia.addons.serializers import AddonSerializer
from olympia.amo.utils import clean_nl, has_links, slug_validator
from olympia.api.fields import (
    SlugOrPrimaryKeyRelatedField, SplitField, TranslationSerializerField)
from olympia.api.utils import is_gate_active
from olympia.bandwagon.models import Collection, CollectionAddon
from olympia.lib.akismet.tasks import save_akismet_report
from olympia.users.models import DeniedName

from .utils import get_collection_akismet_reports


class CollectionAkismetSpamValidator(object):
    def __init__(self, fields):
        self.fields = fields

    def set_context(self, serializer):
        self.serializer = serializer
        self.context = getattr(serializer, 'context', {})

    def __call__(self, attrs):
        data = {
            prop: value for prop, value in attrs.items()
            if prop in self.fields}
        if not data:
            return
        request = self.context.get('request')
        request_meta = getattr(request, 'META', {})
        reports = get_collection_akismet_reports(
            user=getattr(request, 'user', None),
            user_agent=request_meta.get('HTTP_USER_AGENT'),
            referrer=request_meta.get('HTTP_REFERER'),
            collection=self.serializer.instance,
            data=data)
        raise_if_spam = waffle.switch_is_active('akismet-collection-action')
        if any((report.comment_check() for report in reports)):
            # We have to serialize and send it off to a task because the DB
            # transaction will be rolled back because of the ValidationError.
            if raise_if_spam:
                save_akismet_report.delay(object_serialize("json", reports))
                raise serializers.ValidationError(ugettext(
                    'The text entered has been flagged as spam.'))


class CollectionSerializer(serializers.ModelSerializer):
    name = TranslationSerializerField()
    description = TranslationSerializerField(required=False)
    url = serializers.SerializerMethodField()
    author = BaseUserSerializer(default=serializers.CurrentUserDefault())
    public = serializers.BooleanField(source='listed', default=True)
    uuid = serializers.UUIDField(format='hex', required=False)

    class Meta:
        model = Collection
        fields = ('id', 'uuid', 'url', 'addon_count', 'author', 'description',
                  'modified', 'name', 'slug', 'public', 'default_locale')
        writeable_fields = (
            'description', 'name', 'slug', 'public', 'default_locale'
        )
        read_only_fields = tuple(set(fields) - set(writeable_fields))
        validators = [
            UniqueTogetherValidator(
                queryset=Collection.objects.all(),
                message=_(u'This custom URL is already in use by another one '
                          u'of your collections.'),
                fields=('slug', 'author')
            ),
            CollectionAkismetSpamValidator(
                fields=('name', 'description')
            )
        ]

    def get_url(self, obj):
        return obj.get_abs_url()

    def validate_name(self, value):
        # if we have a localised dict of values validate them all.
        if isinstance(value, dict):
            return {locale: self.validate_name(sub_value)
                    for locale, sub_value in six.iteritems(value)}
        if value.strip() == u'':
            raise serializers.ValidationError(
                ugettext(u'Name cannot be empty.'))
        if DeniedName.blocked(value):
            raise serializers.ValidationError(
                ugettext(u'This name cannot be used.'))
        return value

    def validate_description(self, value):
        if has_links(clean_nl(six.text_type(value))):
            # There's some links, we don't want them.
            raise serializers.ValidationError(
                ugettext(u'No links are allowed.'))
        return value

    def validate_slug(self, value):
        slug_validator(
            value, lower=False,
            message=ugettext(u'The custom URL must consist of letters, '
                             u'numbers, underscores or hyphens.'))
        if DeniedName.blocked(value):
            raise serializers.ValidationError(
                ugettext(u'This custom URL cannot be used.'))

        return value


class ThisCollectionDefault(object):
    def set_context(self, serializer_field):
        viewset = serializer_field.context['view']
        self.collection = viewset.get_collection()

    def __call__(self):
        return self.collection


class CollectionAddonSerializer(serializers.ModelSerializer):
    addon = SplitField(
        # Only used for writes (this is input field), so there are no perf
        # concerns and we don't use any special caching.
        SlugOrPrimaryKeyRelatedField(queryset=Addon.objects.public()),
        AddonSerializer())
    notes = TranslationSerializerField(source='comments', required=False)
    collection = serializers.HiddenField(default=ThisCollectionDefault())

    class Meta:
        model = CollectionAddon
        fields = ('addon', 'notes', 'collection')
        validators = [
            UniqueTogetherValidator(
                queryset=CollectionAddon.objects.all(),
                message=_(u'This add-on already belongs to the collection'),
                fields=('addon', 'collection')
            ),
        ]
        writeable_fields = (
            'notes',
        )
        read_only_fields = tuple(set(fields) - set(writeable_fields))

    def validate(self, data):
        if self.partial:
            # addon is read_only but SplitField messes with the initialization.
            # DRF normally ignores updates to read_only fields, so do the same.
            data.pop('addon', None)
        return super(CollectionAddonSerializer, self).validate(data)

    def to_representation(self, instance):
        request = self.context.get('request')
        out = super(
            CollectionAddonSerializer, self).to_representation(instance)
        if request and is_gate_active(request, 'collections-downloads-shim'):
            out['downloads'] = 0
        return out


class CollectionWithAddonsSerializer(CollectionSerializer):
    addons = serializers.SerializerMethodField()

    class Meta(CollectionSerializer.Meta):
        fields = CollectionSerializer.Meta.fields + ('addons',)
        read_only_fields = tuple(
            set(fields) - set(CollectionSerializer.Meta.writeable_fields))

    def get_addons(self, obj):
        addons_qs = self.context['view'].get_addons_queryset()
        return CollectionAddonSerializer(
            addons_qs, context=self.context, many=True).data
