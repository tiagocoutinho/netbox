import svgwrite
from svgwrite.container import Group, Hyperlink
from svgwrite.shapes import Line, Polyline, Rect
from svgwrite.text import Text

from django.conf import settings

from dcim.constants import CABLE_TRACE_SVG_DEFAULT_WIDTH
from utilities.utils import foreground_color


__all__ = (
    'CableTraceSVG',
)


OFFSET = 0.5
PADDING = 10
LINE_HEIGHT = 20
FANOUT_HEIGHT = 35
FANOUT_LEG_HEIGHT = 15


class Node(Hyperlink):
    """
    Create a node to be represented in the SVG document as a rectangular box with a hyperlink.

    Arguments:
        position: (x, y) coordinates of the box's top left corner
        width: Box width
        url: Hyperlink URL
        color: Box fill color (RRGGBB format)
        labels: An iterable of text strings. Each label will render on a new line within the box.
        radius: Box corner radius, for rounded corners (default: 10)
    """

    def __init__(self, position, width, url, color, labels, radius=10, **extra):
        super(Node, self).__init__(href=url, target='_parent', **extra)

        x, y = position

        # Add the box
        dimensions = (width - 2, PADDING + LINE_HEIGHT * len(labels) + PADDING)
        box = Rect((x + OFFSET, y), dimensions, rx=radius, class_='parent-object', style=f'fill: #{color}')
        self.add(box)

        cursor = y + PADDING

        # Add text label(s)
        for i, label in enumerate(labels):
            cursor += LINE_HEIGHT
            text_coords = (x + width / 2, cursor - LINE_HEIGHT / 2)
            text_color = f'#{foreground_color(color, dark="303030")}'
            text = Text(label, insert=text_coords, fill=text_color, class_='bold' if not i else [])
            self.add(text)

    @property
    def box(self):
        return self.elements[0] if self.elements else None

    @property
    def top_center(self):
        return self.box['x'] + self.box['width'] / 2, self.box['y']

    @property
    def bottom_center(self):
        return self.box['x'] + self.box['width'] / 2, self.box['y'] + self.box['height']


class Connector(Group):
    """
    Return an SVG group containing a line element and text labels representing a Cable.

    Arguments:
        color: Cable (line) color
        url: Hyperlink URL
        labels: Iterable of text labels
    """

    def __init__(self, start, url, color, labels=[], **extra):
        super().__init__(class_='connector', **extra)

        self.start = start
        self.height = PADDING * 2 + LINE_HEIGHT * len(labels) + PADDING * 2
        self.end = (start[0], start[1] + self.height)
        self.color = color or '000000'

        # Draw a "shadow" line to give the cable a border
        cable_shadow = Line(start=self.start, end=self.end, class_='cable-shadow')
        self.add(cable_shadow)

        # Draw the cable
        cable = Line(start=self.start, end=self.end, style=f'stroke: #{self.color}')
        self.add(cable)

        # Add link
        link = Hyperlink(href=url, target='_parent')

        # Add text label(s)
        cursor = start[1]
        cursor += PADDING * 2
        for i, label in enumerate(labels):
            cursor += LINE_HEIGHT
            text_coords = (start[0] + PADDING * 2, cursor - LINE_HEIGHT / 2)
            text = Text(label, insert=text_coords, class_='bold' if not i else [])
            link.add(text)

        self.add(link)


class CableTraceSVG:
    """
    Generate a graphical representation of a CablePath in SVG format.

    :param origin: The originating termination
    :param width: Width of the generated image (in pixels)
    :param base_url: Base URL for links within the SVG document. If none, links will be relative.
    """
    def __init__(self, origin, width=CABLE_TRACE_SVG_DEFAULT_WIDTH, base_url=None):
        self.origin = origin
        self.width = width
        self.base_url = base_url.rstrip('/') if base_url is not None else ''

        # Establish a cursor to track position on the y axis
        # Center edges on pixels to render sharp borders
        self.cursor = OFFSET

        # Prep elements lists
        self.parent_objects = []
        self.terminations = []
        self.connectors = []

    @property
    def center(self):
        return self.width / 2

    @classmethod
    def _get_labels(cls, instance):
        """
        Return a list of text labels for the given instance based on model type.
        """
        labels = [str(instance)]
        if instance._meta.model_name == 'device':
            labels.append(f'{instance.device_type.manufacturer} {instance.device_type}')
            location_label = f'{instance.site}'
            if instance.location:
                location_label += f' / {instance.location}'
            if instance.rack:
                location_label += f' / {instance.rack}'
            labels.append(location_label)
        elif instance._meta.model_name == 'circuit':
            labels[0] = f'Circuit {instance}'
            labels.append(instance.provider)
        elif instance._meta.model_name == 'circuittermination':
            if instance.xconnect_id:
                labels.append(f'{instance.xconnect_id}')
        elif instance._meta.model_name == 'providernetwork':
            labels.append(instance.provider)

        return labels

    @classmethod
    def _get_color(cls, instance):
        """
        Return the appropriate fill color for an object within a cable path.
        """
        if hasattr(instance, 'parent_object'):
            # Termination
            return 'f0f0f0'
        if hasattr(instance, 'device_role'):
            # Device
            return instance.device_role.color
        else:
            # Other parent object
            return 'e0e0e0'

    def draw_parent_objects(self, obj_list):
        """
        Draw a set of parent objects.
        """
        width = self.width / len(obj_list)
        for i, obj in enumerate(obj_list):
            node = Node(
                position=(i * width, self.cursor),
                width=width,
                url=f'{self.base_url}{obj.get_absolute_url()}',
                color=self._get_color(obj),
                labels=self._get_labels(obj)
            )
            self.parent_objects.append(node)
            if i + 1 == len(obj_list):
                self.cursor += node.box['height']

    def draw_terminations(self, terminations):
        """
        Draw a row of terminating objects (e.g. interfaces), all of which are attached to the same end of a cable.
        """
        nodes = []
        nodes_height = 0
        width = self.width / len(terminations)

        for i, term in enumerate(terminations):
            node = Node(
                position=(i * width, self.cursor),
                width=width,
                url=f'{self.base_url}{term.get_absolute_url()}',
                color=self._get_color(term),
                labels=self._get_labels(term),
                radius=5
            )
            nodes_height = max(nodes_height, node.box['height'])
            nodes.append(node)

        self.cursor += nodes_height
        self.terminations.extend(nodes)

        return nodes

    def draw_fanin(self, node, connector):
        points = (
            node.bottom_center,
            (node.bottom_center[0], node.bottom_center[1] + FANOUT_LEG_HEIGHT),
            connector.start,
        )
        self.connectors.extend((
            Polyline(points=points, class_='cable-shadow'),
            Polyline(points=points, style=f'stroke: #{connector.color}'),
        ))

    def draw_fanout(self, node, connector):
        points = (
            connector.end,
            (node.top_center[0], node.top_center[1] - FANOUT_LEG_HEIGHT),
            node.top_center,
        )
        self.connectors.extend((
            Polyline(points=points, class_='cable-shadow'),
            Polyline(points=points, style=f'stroke: #{connector.color}'),
        ))

    def draw_cable(self, cable):
        labels = [
            f'Cable {cable}',
            cable.get_status_display()
        ]
        if cable.type:
            labels.append(cable.get_type_display())
        if cable.length and cable.length_unit:
            labels.append(f'{cable.length} {cable.get_length_unit_display()}')
        connector = Connector(
            start=(self.center + OFFSET, self.cursor),
            color=cable.color or '000000',
            url=f'{self.base_url}{cable.get_absolute_url()}',
            labels=labels
        )

        self.cursor += connector.height

        return connector

    def draw_wirelesslink(self, wirelesslink):
        """
        Draw a line with labels representing a WirelessLink.
        """
        group = Group(class_='connector')

        labels = [
            f'Wireless link {wirelesslink}',
            wirelesslink.get_status_display()
        ]
        if wirelesslink.ssid:
            labels.append(wirelesslink.ssid)

        # Draw the wireless link
        start = (OFFSET + self.center, self.cursor)
        height = PADDING * 2 + LINE_HEIGHT * len(labels) + PADDING * 2
        end = (start[0], start[1] + height)
        line = Line(start=start, end=end, class_='wireless-link')
        group.add(line)

        self.cursor += PADDING * 2

        # Add link
        link = Hyperlink(href=f'{self.base_url}{wirelesslink.get_absolute_url()}', target='_parent')

        # Add text label(s)
        for i, label in enumerate(labels):
            self.cursor += LINE_HEIGHT
            text_coords = (self.center + PADDING * 2, self.cursor - LINE_HEIGHT / 2)
            text = Text(label, insert=text_coords, class_='bold' if not i else [])
            link.add(text)

        group.add(link)
        self.cursor += PADDING * 2

        return group

    def draw_attachment(self):
        """
        Return an SVG group containing a line element and "Attachment" label.
        """
        group = Group(class_='connector')

        # Draw attachment (line)
        start = (OFFSET + self.center, OFFSET + self.cursor)
        height = PADDING * 2 + LINE_HEIGHT + PADDING * 2
        end = (start[0], start[1] + height)
        line = Line(start=start, end=end, class_='attachment')
        group.add(line)
        self.cursor += PADDING * 4

        return group

    def render(self):
        """
        Return an SVG document representing a cable trace.
        """
        from dcim.models import Cable
        from wireless.models import WirelessLink

        traced_path = self.origin.trace()

        # Iterate through each (terms, cable, terms) segment in the path
        for i, segment in enumerate(traced_path):
            near_ends, links, far_ends = segment

            # Near end parent
            if i == 0:
                # If this is the first segment, draw the originating termination's parent object
                self.draw_parent_objects(set(end.parent_object for end in near_ends))

            # Near end termination(s)
            terminations = self.draw_terminations(near_ends)

            # Connector (a Cable or WirelessLink)
            if links:
                link = links[0]  # Remove Cable from list

                # Cable
                if type(link) is Cable:

                    # Account for fan-ins height
                    if len(near_ends) > 1:
                        self.cursor += FANOUT_HEIGHT

                    cable = self.draw_cable(link)
                    self.connectors.append(cable)

                    # Draw fan-ins
                    if len(near_ends) > 1:
                        for term in terminations:
                            self.draw_fanin(term, cable)

                # WirelessLink
                elif type(link) is WirelessLink:
                    wirelesslink = self.draw_wirelesslink(link)
                    self.connectors.append(wirelesslink)

                # Far end termination(s)
                if len(far_ends) > 1:
                    self.cursor += FANOUT_HEIGHT
                    terminations = self.draw_terminations(far_ends)
                    for term in terminations:
                        self.draw_fanout(term, cable)
                elif far_ends:
                    self.draw_terminations(far_ends)
                else:
                    # Link is not connected to anything
                    break

                # Far end parent
                parent_objects = set(end.parent_object for end in far_ends)
                self.draw_parent_objects(parent_objects)

            # Render a far-end object not connected via a link (e.g. a ProviderNetwork or Site associated with
            # a CircuitTermination)
            elif far_ends:

                # Attachment
                attachment = self.draw_attachment()
                self.connectors.append(attachment)

                # Object
                self.draw_parent_objects(far_ends)

        # Determine drawing size
        self.drawing = svgwrite.Drawing(
            size=(self.width, self.cursor + 2)
        )

        # Attach CSS stylesheet
        with open(f'{settings.STATIC_ROOT}/cable_trace.css') as css_file:
            self.drawing.defs.add(self.drawing.style(css_file.read()))

        # Add elements to the drawing in order of depth (Z axis)
        for element in self.connectors + self.parent_objects + self.terminations:
            self.drawing.add(element)

        return self.drawing
