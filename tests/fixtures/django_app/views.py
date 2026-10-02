from rest_framework import serializers, viewsets


class PlotSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    archived = serializers.BooleanField(required=False, allow_null=True)


class PlotViewSet(viewsets.ModelViewSet):
    serializer_class = PlotSerializer


def report_detail(request, report_id):
    pass


def export_list(request):
    pass
