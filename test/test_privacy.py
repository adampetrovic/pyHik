"""Tests for full-frame privacy masks; no real devices are used."""

import threading
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch

import requests
from requests.auth import HTTPDigestAuth

from pyhik.hikvision import HikCamera
from pyhik.privacy import PrivacyMaskError, get_privacy_mode, set_privacy_mode

NS = '{http://www.hikvision.com/ver20/XMLSchema}'
ROOT_URL = 'https://camera.example:8443'
URL = ROOT_URL + '/ISAPI/System/Video/inputs/channels/3/privacyMask'
SUCCESS = b'<ResponseStatus><statusCode>1</statusCode></ResponseStatus>'


def response(body=b'', status=200):
    result = requests.Response()
    result.status_code = status
    result._content = body
    return result


def privacy_xml(enabled=False, width=704, height=576, regions=(), capacity=4):
    root = ET.Element(NS + 'PrivacyMask', {'version': '2.0'})
    ET.SubElement(root, NS + 'enabled').text = str(enabled).lower()
    size = ET.SubElement(root, NS + 'normalizedScreenSize')
    ET.SubElement(size, NS + 'normalizedScreenWidth').text = str(width)
    ET.SubElement(size, NS + 'normalizedScreenHeight').text = str(height)
    region_list = ET.SubElement(root, NS + 'PrivacyMaskRegionList', {'size': str(capacity)})
    for region_id, active, points in regions:
        region = ET.SubElement(region_list, NS + 'PrivacyMaskRegion')
        ET.SubElement(region, NS + 'id').text = str(region_id)
        ET.SubElement(region, NS + 'enabled').text = str(active).lower()
        coordinates = ET.SubElement(region, NS + 'RegionCoordinatesList')
        for x, y in points:
            point = ET.SubElement(coordinates, NS + 'RegionCoordinates')
            ET.SubElement(point, NS + 'positionX').text = str(x)
            ET.SubElement(point, NS + 'positionY').text = str(y)
    ET.SubElement(root, NS + 'deviceSpecificSetting').text = 'keep'
    return ET.tostring(root)


def fullframe(region_id=1, active=True, width=704, height=576):
    return region_id, active, ((0, 0), (width, 0), (width, height), (0, height))


def partial(region_id=1):
    return region_id, True, ((10, 10), (20, 10), (20, 20), (10, 20))


class PrivacyMaskTestCase(unittest.TestCase):
    def setUp(self):
        self.session = MagicMock()
        self.session.get.return_value = response(privacy_xml())
        self.session.put.return_value = response(SUCCESS)

    def get(self, body):
        self.session.get.return_value = response(body)
        return get_privacy_mode(self.session, ROOT_URL, 3)

    def test_states(self):
        for body, expected in (
            (privacy_xml(), False),
            (privacy_xml(True, regions=[fullframe()]), True),
            (privacy_xml(True, regions=[partial()]), False),
            (privacy_xml(True, regions=[fullframe(active=False)]), False),
            (privacy_xml(True, regions=[fullframe()]).replace(NS[1:-1].encode(), b''), True),
        ):
            with self.subTest(body=body):
                self.assertEqual(self.get(body), expected)
        self.session.get.assert_called_with(URL, timeout=10)

    def test_polygon_order_and_multiple_fullframe_regions(self):
        corners = list(fullframe()[2])
        for order in (corners, list(reversed(corners))):
            for offset in range(4):
                with self.subTest(order=order, offset=offset):
                    rotated = order[offset:] + order[:offset]
                    self.assertTrue(self.get(privacy_xml(True, regions=[(1, True, rotated)])))
        crossed = [corners[0], corners[2], corners[1], corners[3]]
        self.assertFalse(self.get(privacy_xml(True, regions=[(1, True, crossed)])))
        self.assertTrue(self.get(privacy_xml(True, regions=[fullframe(active=False), fullframe(2)])))
        self.assertFalse(self.get(privacy_xml(True, regions=[fullframe()]).replace(
            b'</PrivacyMaskRegion>', b'<maskType>transparent</maskType></PrivacyMaskRegion>')))

    def test_invalid_configuration(self):
        for body in (
            b'<broken', b'<OtherDocument/>',
            privacy_xml().replace(b'>false<', b'>maybe<'),
            privacy_xml(width='invalid'), privacy_xml(width=0),
            b'<PrivacyMask><enabled>false</enabled></PrivacyMask>',
        ):
            with self.subTest(body=body), self.assertRaises(PrivacyMaskError):
                self.get(body)

    def test_unsupported(self):
        for status in (404, 405, 501):
            with self.subTest(status=status):
                self.session.get.return_value = response(status=status)
                self.assertIsNone(get_privacy_mode(self.session, ROOT_URL, 3))
        self.assertIsNone(self.get(b'<ResponseStatus><subStatusCode>notSupport</subStatusCode></ResponseStatus>'))

    def test_http_failures(self):
        for status in (401, 403, 500):
            with self.subTest(status=status), self.assertRaises(requests.HTTPError):
                self.session.get.return_value = response(status=status)
                get_privacy_mode(self.session, ROOT_URL, 3)
        self.session.get.side_effect = requests.Timeout()
        with self.assertRaises(requests.Timeout):
            get_privacy_mode(self.session, ROOT_URL, 3)

    def write_and_readback(self, original, enabled):
        stored = [original]
        self.session.get.side_effect = lambda *args, **kwargs: response(stored[0])

        def put(url, **kwargs):
            stored[0] = kwargs['data']
            return response(SUCCESS)

        self.session.put.side_effect = put
        self.assertEqual(set_privacy_mode(self.session, ROOT_URL, 3, enabled), enabled)
        self.assertEqual(self.session.put.call_args.args, (URL,))
        self.assertEqual(self.session.put.call_args.kwargs['timeout'], 10)
        self.assertEqual(self.session.put.call_args.kwargs['headers']['Content-Type'], 'application/xml')
        return ET.fromstring(stored[0])

    def test_enable_uses_device_dimensions(self):
        root = self.write_and_readback(privacy_xml(width=1024, height=768), True)
        self.assertEqual(root.findtext(NS + 'enabled'), 'true')
        region = root.find(NS + 'PrivacyMaskRegionList/' + NS + 'PrivacyMaskRegion')
        self.assertEqual(region.findtext(NS + 'id'), '1')
        points = region.findall(NS + 'RegionCoordinatesList/' + NS + 'RegionCoordinates')
        self.assertEqual([(p.findtext(NS + 'positionX'), p.findtext(NS + 'positionY')) for p in points],
                         [('0', '0'), ('1024', '0'), ('1024', '768'), ('0', '768')])
        self.assertEqual(root.findtext(NS + 'deviceSpecificSetting'), 'keep')

    def test_enable_preserves_partial_regions(self):
        original = ET.fromstring(privacy_xml(regions=[partial()]))
        original_region = original.find(NS + 'PrivacyMaskRegionList/' + NS + 'PrivacyMaskRegion')
        root = self.write_and_readback(ET.tostring(original), True)
        regions = root.findall(NS + 'PrivacyMaskRegionList/' + NS + 'PrivacyMaskRegion')
        self.assertEqual([r.findtext(NS + 'id') for r in regions], ['1', '2'])
        self.assertEqual(ET.tostring(regions[0]), ET.tostring(original_region))

    def test_reuses_fullframe(self):
        root = self.write_and_readback(privacy_xml(regions=[fullframe(active=False)]), True)
        regions = root.findall(NS + 'PrivacyMaskRegionList/' + NS + 'PrivacyMaskRegion')
        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0].findtext(NS + 'enabled'), 'true')

    def test_disable_preserves_regions(self):
        original = privacy_xml(True, regions=[partial(), fullframe(2)])
        root = self.write_and_readback(original, False)
        self.assertEqual(root.findtext(NS + 'enabled'), 'false')
        self.assertEqual(ET.tostring(root.find(NS + 'PrivacyMaskRegionList')),
                         ET.tostring(ET.fromstring(original).find(NS + 'PrivacyMaskRegionList')))

    def test_capacity_and_unsupported_write(self):
        for original in (response(privacy_xml(regions=[partial()], capacity=1)), response(status=404)):
            with self.subTest(original=original), self.assertRaises(PrivacyMaskError):
                self.session.get.return_value = original
                set_privacy_mode(self.session, ROOT_URL, 3, True)
        self.session.put.assert_not_called()

    def test_rejected_write(self):
        for body in (b'<broken', b'<ResponseStatus><statusCode>2</statusCode></ResponseStatus>'):
            with self.subTest(body=body), self.assertRaises(PrivacyMaskError):
                self.session.put.return_value = response(body)
                set_privacy_mode(self.session, ROOT_URL, 3, True)
        self.session.put.return_value = response(status=500)
        with self.assertRaises(requests.HTTPError):
            set_privacy_mode(self.session, ROOT_URL, 3, True)

    def test_readback_not_confirmed(self):
        with self.assertRaises(PrivacyMaskError):
            set_privacy_mode(self.session, ROOT_URL, 3, True)
        self.session.get.side_effect = [response(privacy_xml()), requests.Timeout()]
        with self.assertRaises(requests.Timeout):
            set_privacy_mode(self.session, ROOT_URL, 3, True)

    def test_readback_preserves_existing_regions(self):
        self.session.get.side_effect = [
            response(privacy_xml(regions=[partial()])),
            response(privacy_xml(True, regions=[fullframe(2)])),
        ]
        with self.assertRaises(PrivacyMaskError):
            set_privacy_mode(self.session, ROOT_URL, 3, True)

    def test_unknown_capacity_rejected(self):
        self.session.get.return_value = response(privacy_xml().replace(b' size="4"', b''))
        with self.assertRaises(PrivacyMaskError):
            set_privacy_mode(self.session, ROOT_URL, 3, True)
        self.session.put.assert_not_called()

    def test_invalid_channel(self):
        with self.assertRaises(ValueError):
            get_privacy_mode(self.session, ROOT_URL, 0)
        self.session.get.assert_not_called()


class HikCameraPrivacyTestCase(unittest.TestCase):
    def test_isolated_session(self):
        for auth in (('user', 'password'), HTTPDigestAuth('user', 'password')):
            with self.subTest(auth=auth), patch('pyhik.hikvision.requests.Session') as session_class:
                camera = object.__new__(HikCamera)
                camera.root_url, camera.usr, camera.pwd = ROOT_URL, 'user', 'password'
                camera.hik_request = MagicMock(auth=auth, verify=True)
                camera._privacy_lock = threading.Lock()
                isolated = session_class.return_value
                isolated.__enter__.return_value = isolated
                with patch('pyhik.hikvision.get_privacy_mode', return_value=True) as get:
                    self.assertTrue(camera.get_privacy_mode(3))
                    get.assert_called_once_with(isolated, ROOT_URL, 3)
                self.assertTrue(isolated.verify)
                if isinstance(auth, HTTPDigestAuth):
                    self.assertIsInstance(isolated.auth, HTTPDigestAuth)
                    self.assertIsNot(isolated.auth, auth)
                    self.assertEqual(isolated.auth.username, 'user')
                else:
                    self.assertEqual(isolated.auth, auth)
                with patch('pyhik.hikvision.set_privacy_mode', return_value=False) as put:
                    self.assertFalse(camera.set_privacy_mode(3, False))
                    put.assert_called_once_with(isolated, ROOT_URL, 3, False)
                isolated.__exit__.assert_called()
