import octoprint.plugin
from urllib.request import Request, urlopen
from socket import gethostbyname, gethostname
from threading import Thread
from time import time, sleep
from threading import Lock
from json import loads, dumps
from base64 import b64encode
from uuid import uuid4
import io
import PIL.Image as Image
from PIL import ImageDraw
import re
import sys
from memory_profiler import profile


DEFAULT_ROUTE = 'http://printwatch-printpal.pythonanywhere.com'

class CommManager(octoprint.plugin.SettingsPlugin):
    @profile
    def __init__(self, plugin):
        self.plugin = plugin
        self.heartbeat_interval = 30.0
        self.parameters = {
                            'last_t' : 0.0,
                            'ip' : gethostbyname(gethostname()),
                            'route' : DEFAULT_ROUTE,
                            'nms' : False,
                            'id' : self.plugin._settings.global_get(["accessControl", "salt"]) if self.plugin._settings.global_get(["accessControl", "salt"]) is not None else uuid4().hex,
                            'bad_responses' : 0
                            }

    @profile
    def _heartbeat(self):
        while self.plugin._settings.get(["enable_detector"]) and self.heartbeat:
            sleep(0.1) #prevent cpu overload
            if time() - self.parameters['last_t'] > self.heartbeat_interval:
                try:
                    response = self._send(heartbeat=True)
                    self._check_action(response)
                except Exception as e:
                    self.plugin._logger.info("Error with Heartbeat: {}".format(str(e)))

                self.parameters['last_t'] = time()
        self.plugin._logger.info("Heartbeat loop closed")


    @profile
    def _create_payload(self, image=None):
        settings = self.plugin._settings.get([])
        if not "confidence" in settings:
            settings["confidence"] = 60
        return dumps({
                            'image_array' : image,
                            'settings' : settings,
                            'parameters' : self.parameters,
                            'job' : self.plugin._printer.get_current_job(),
                            'data' : self.plugin._printer.get_current_data(),
                            'state' : self.plugin._printer.get_state_id()
                            }).encode('utf8')

    @profile
    def _send(self, heartbeat=False):
        if heartbeat:
            data = self._create_payload()
        else:
            data = self._create_payload(b64encode(self.image).decode('utf8'))

        inference_request = Request('{}/inference/'.format(
            self.parameters['route']),
            data=data,
            method='POST'
        )

        return loads(urlopen(inference_request).read())
    @profile
    def _check_action(self, response):
        if response['actionType'] == 'pause':
            while not ((self.plugin._printer.is_pausing() and self.plugin._printer.is_printing()) or self.plugin._printer.is_paused()):
                self.plugin._printer.pause_print()
        elif response['actionType'] == 'stop':
            while not (self.plugin._printer.is_cancelling() and self.plugin._printer.is_printing()):
                self.plugin._printer.cancel_print()
        elif response['actionType'] == 'resume':
            if self.plugin._printer.is_paused():
                while not self.plugin._printer.is_printing():
                    self.plugin._printer.resume_print()
    @profile
    def start_service(self):
        self.heartbeat = True
        if self.plugin._settings.get(["enable_detector"]):
            if self.plugin.inferencer.inference_loop is None and self.plugin.streamer.stream is None:
                self.heartbeat_loop = Thread(target=self._heartbeat)
                self.heartbeat_loop.daemon = True
                self.heartbeat_loop.start()
                self.plugin._logger.info("PrintWatch heartbeat service started")
    @profile
    def kill_service(self):
        self.heartbeat = False
        self.heartbeat_loop = None
        self.plugin._logger.info("PrintWatch heartbeat service terminated")
    @profile
    def send_request(self):
        with Lock():
            self.image = bytearray(self.plugin.streamer.jpg)
        self.image_memory_size = sys.getsizeof(self.image)
        try:
            response = self._send()
            self.parameters['last_t'] = time()
            self.parameters_size = sys.getsizeof(self.parameters)
            if response['statusCode'] == 200:
                self.plugin.inferencer.pred = eval(response['defect_detected'])
                self.pred_size = sys.getsizeof(self.plugin.inferencer.pred)
                self.parameters['bad_responses'] = 0
                self.plugin.inferencer.REQUEST_INTERVAL = 10.0
                boxes = eval(re.sub('\s+', ',', re.sub('\s+\]', ']', re.sub('\[\s+', '[', response['boxes'].replace('\n','')))))
                self.plugin._plugin_manager.send_plugin_message(self.plugin._identifier, dict(type="display_frame", image=self.draw_boxes(boxes)))
                self.plugin._plugin_manager.send_plugin_message(self.plugin._identifier, dict(type="icon", icon='plugin/printwatch/static/img/printwatch-green.gif'))
                self._check_action(response)
            elif response['statusCode'] == 213:
                self.plugin.inferencer.REQUEST_INTERVAL= 300.0
            else:
                self.plugin.inferencer.pred = False
                self.parameters['bad_responses'] += 1
                self.plugin.inferencer.REQUEST_INTERVAL = 10.0
                self.plugin._logger.info("Payload: {} {}".format(self.plugin._settings.get([]), self.parameters))
                self.plugin._logger.info("Response: {}".format(response))

        except Exception as e:
            self.plugin._logger.info("Error retrieving server response: {}".format(str(e)))
            self.parameters['bad_responses'] += 1
            self.plugin.inferencer.pred = False
            self.parameters['last_t'] = time()
    @profile
    def draw_boxes(self, boxes):
        pil_img = Image.open(io.BytesIO(self.image))
        process_image = ImageDraw.Draw(pil_img)
        width, height = pil_img.size

        for i, det in enumerate(boxes):
            det = [j / 640 for j in det]
            x1 = (det[0] - (det[2]/2))*width
            y1 = (det[1] - (det[3]/2))* height
            x2 = (det[0] + (det[2]/2))*width
            y2 = (det[1] + (det[3]/2))*height
            process_image.rectangle([(x1, y1), (x2, y2)], fill=None, outline="red", width=4)

        out_img = io.BytesIO()
        pil_img.save(out_img, format='PNG')
        contents = b64encode(out_img.getvalue()).decode('utf8')
        return 'data:image/png;charset=utf-8;base64,' + contents.split('\n')[0]
    @profile
    def email_notification(self):
        if self.plugin._settings.get(["enable_email_notification"]):
            self.parameters['nms'] = True
            sleep(self.plugin.inferencer.REQUEST_INTERVAL)
            self.send_request()
            self.plugin._logger.info("Email notification sent to {}".format(self.plugin._settings.get(["email_addr"])))
