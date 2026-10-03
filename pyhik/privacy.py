"""Channel privacy mode using Hikvision's full-frame privacy masks."""

import xml.etree.ElementTree as ET
from typing import Optional

TIMEOUT = 10
UNSUPPORTED_STATUS_CODES = {404, 405, 501}


class PrivacyMaskError(ValueError):
    """The device returned invalid configuration or rejected privacy mode."""


def _configuration(session, root_url, channel):
    if not isinstance(channel, int) or channel < 1:
        raise ValueError('Channel must be a positive integer')
    url = f'{root_url}/ISAPI/System/Video/inputs/channels/{channel}/privacyMask'
    response = session.get(url, timeout=TIMEOUT)
    if response.status_code in UNSUPPORTED_STATUS_CODES:
        return url, None
    response.raise_for_status()
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as err:
        raise PrivacyMaskError('Malformed privacy mask response') from err
    namespace = root.tag.partition('}')[0] + '}' if root.tag.startswith('{') else ''
    if root.tag == f'{namespace}ResponseStatus':
        if root.findtext(f'{namespace}subStatusCode') in {'notSupport', 'notSupported'}:
            return url, None
        raise PrivacyMaskError('Device rejected privacy mask request')
    if root.tag != f'{namespace}PrivacyMask':
        raise PrivacyMaskError('Unexpected privacy mask response')
    if root.findtext(f'{namespace}enabled') not in {'true', 'false'}:
        raise PrivacyMaskError('Invalid privacy mask enabled state')
    return url, root


def _namespace(root):
    return root.tag.partition('}')[0] + '}' if root.tag.startswith('{') else ''


def _dimensions(root):
    ns = _namespace(root)
    size = root.find(f'{ns}normalizedScreenSize')
    if size is None:
        raise PrivacyMaskError('Device does not report privacy mask dimensions')
    try:
        width = int(size.findtext(f'{ns}normalizedScreenWidth', ''))
        height = int(size.findtext(f'{ns}normalizedScreenHeight', ''))
    except ValueError as err:
        raise PrivacyMaskError('Invalid privacy mask dimensions') from err
    if width <= 0 or height <= 0:
        raise PrivacyMaskError('Invalid privacy mask dimensions')
    return width, height


def _full_frame_regions(root):
    ns = _namespace(root)
    width, height = _dimensions(root)
    corners = [(0, 0), (width, 0), (width, height), (0, height)]
    perimeter_orders = [order[offset:] + order[:offset]
                        for order in (corners, list(reversed(corners)))
                        for offset in range(4)]
    matches = []
    for region in root.findall(f'{ns}PrivacyMaskRegionList/{ns}PrivacyMaskRegion'):
        points = region.findall(f'{ns}RegionCoordinatesList/{ns}RegionCoordinates')
        try:
            coordinates = [
                (int(point.findtext(f'{ns}positionX', '')),
                 int(point.findtext(f'{ns}positionY', '')))
                for point in points
            ]
        except ValueError:
            continue
        if (coordinates in perimeter_orders
                and region.findtext(f'{ns}maskType') in (None, 'gray', 'black')):
            matches.append(region)
    return matches


def _enabled(root):
    ns = _namespace(root)
    return (root.findtext(f'{ns}enabled') == 'true'
            and any(region.findtext(f'{ns}enabled') == 'true'
                    for region in _full_frame_regions(root)))


def _region_signatures(root):
    def signature(element):
        return (element.tag, tuple(sorted(element.attrib.items())),
                (element.text or '').strip(),
                tuple(signature(child) for child in element))
    ns = _namespace(root)
    return {region.findtext(f'{ns}id'): signature(region)
            for region in root.findall(f'{ns}PrivacyMaskRegionList/{ns}PrivacyMaskRegion')}


def get_privacy_mode(session, root_url: str, channel: int = 1) -> Optional[bool]:
    """Return full-frame privacy state, or None for an unsupported endpoint."""
    _, root = _configuration(session, root_url, channel)
    if root is None:
        return None
    _dimensions(root)
    return _enabled(root)


def set_privacy_mode(session, root_url: str, channel: int, enabled: bool) -> bool:
    """Enable full-frame masking or disable masking, then verify device state.

    Existing regions and other device-specific XML are preserved. Enabling may
    add a full-frame region, which remains configured when privacy is disabled.
    Never replace a partial mask to make room for a full-frame mask.
    """
    url, root = _configuration(session, root_url, channel)
    if root is None:
        raise PrivacyMaskError('Privacy masks are not supported on this channel')
    ns = _namespace(root)
    if enabled:
        width, height = _dimensions(root)
        full_frame_regions = _full_frame_regions(root)
        region = next((item for item in full_frame_regions
                       if item.findtext(f'{ns}enabled') == 'true'),
                      full_frame_regions[0] if full_frame_regions else None)
        if region is None:
            regions = root.find(f'{ns}PrivacyMaskRegionList')
            if regions is None:
                raise PrivacyMaskError('Device does not report privacy mask regions')
            try:
                capacity = int(regions.get('size', ''))
                used_ids = {
                    int(item.findtext(f'{ns}id', ''))
                    for item in regions.findall(f'{ns}PrivacyMaskRegion')
                }
            except ValueError as err:
                raise PrivacyMaskError('Invalid privacy mask region list') from err
            available = [number for number in range(1, capacity + 1)
                         if number not in used_ids]
            if not available:
                raise PrivacyMaskError('No free privacy mask region for full-frame privacy')
            region = ET.SubElement(regions, f'{ns}PrivacyMaskRegion')
            ET.SubElement(region, f'{ns}id').text = str(available[0])
            ET.SubElement(region, f'{ns}enabled').text = 'true'
            coordinates = ET.SubElement(region, f'{ns}RegionCoordinatesList')
            for x, y in ((0, 0), (width, 0), (width, height), (0, height)):
                point = ET.SubElement(coordinates, f'{ns}RegionCoordinates')
                ET.SubElement(point, f'{ns}positionX').text = str(x)
                ET.SubElement(point, f'{ns}positionY').text = str(y)
        region_enabled = region.find(f'{ns}enabled')
        if region_enabled is None:
            raise PrivacyMaskError('Invalid privacy mask region enabled state')
        region_enabled.text = 'true'
    enabled_element = root.find(f'{ns}enabled')
    if enabled_element is None:
        raise PrivacyMaskError('Missing privacy mask enabled state')
    enabled_element.text = 'true' if enabled else 'false'
    expected_regions = _region_signatures(root)
    expected_dimensions = _dimensions(root)
    response = session.put(
        url, data=ET.tostring(root, encoding='utf-8', xml_declaration=True),
        headers={'Content-Type': 'application/xml'}, timeout=TIMEOUT,
    )
    response.raise_for_status()
    try:
        result = ET.fromstring(response.content)
    except ET.ParseError as err:
        raise PrivacyMaskError('Malformed privacy mask write response') from err
    result_ns = _namespace(result)
    if (result.tag != f'{result_ns}ResponseStatus'
            or result.findtext(f'{result_ns}statusCode') != '1'):
        raise PrivacyMaskError('Device rejected privacy mask change')
    _, actual = _configuration(session, root_url, channel)
    if (actual is None
            or actual.findtext(f'{_namespace(actual)}enabled') != ('true' if enabled else 'false')
            or _enabled(actual) != enabled
            or _dimensions(actual) != expected_dimensions
            or any(_region_signatures(actual).get(region_id) != region
                   for region_id, region in expected_regions.items())):
        raise PrivacyMaskError('Device did not confirm the requested privacy mode')
    return enabled
