import base64
import pickle
import re
from os import path

from py12306.cluster.cluster import Cluster
from py12306.helpers.api import *
from py12306.app import *
from py12306.helpers.auth_code import AuthCode
from py12306.helpers.event import Event
from py12306.helpers.func import *
from py12306.helpers.request import Request
from py12306.helpers.type import UserType
from py12306.helpers.qrcode import print_qrcode
from py12306.log.order_log import OrderLog
from py12306.log.user_log import UserLog
from py12306.log.common_log import CommonLog
from py12306.order.order import Browser


class UserJob:
    # heartbeat = 60 * 2  # 心跳保持时长
    is_alive = True
    check_interval = 5
    key = None
    user_name = ''
    password = ''
    user_card = ''
    type = 'qr'
    user = None
    info = {}  # 用户信息
    last_heartbeat = None
    is_ready = False
    user_loaded = False  # 用户是否已加载成功
    passengers = []
    retry_time = 3
    retry_count = 0
    login_num = 0  # 尝试登录次数
    sleep_interval = {'min': 0.1, 'max': 5}

    # Init page
    global_repeat_submit_token = None
    ticket_info_for_passenger_form = None
    order_request_dto = None

    cluster = None
    lock_init_user_time = 3 * 60
    cookie = False

    def __init__(self, info):
        self.cluster = Cluster()
        self.init_data(info)

    def init_data(self, info):
        self.session = Request()
        self.session.add_response_hook(self.response_login_check)
        self.key = str(info.get('key'))
        self.user_name = info.get('user_name')
        self.password = info.get('password')
        self.user_card = info.get('user_card')
        self.type = info.get('type')

    def update_user(self):
        from py12306.user.user import User
        self.user = User()
        self.load_user()

    def run(self):
        # load user
        self.update_user()
        self.start()

    def start(self):
        """
        检测心跳
        :return:
        """
        while True and self.is_alive:
            app_available_check()
            if Config().is_slave():
                self.load_user_from_remote()
            else:
                if Config().is_master() and not self.cookie: self.load_user_from_remote()  # 主节点加载一次 Cookie
                self.check_heartbeat()
            if Const.IS_TEST: return
            stay_second(self.check_interval)

    def check_heartbeat(self):
        # 心跳检测
        if self.get_last_heartbeat() and (time_int() - self.get_last_heartbeat()) < Config().USER_HEARTBEAT_INTERVAL:
            return True
        # 只有主节点才能走到这
        if self.is_first_time() or not self.check_user_is_login() or not self.can_access_passengers():
            if not self.load_user() and not self.handle_login(): return

        self.user_did_load()
        message = UserLog.MESSAGE_USER_HEARTBEAT_NORMAL.format(self.get_name(), Config().USER_HEARTBEAT_INTERVAL)
        UserLog.add_quick_log(message).flush()

    def get_last_heartbeat(self):
        if Config().is_cluster_enabled():
            return int(self.cluster.session.get(Cluster.KEY_USER_LAST_HEARTBEAT, 0))

        return self.last_heartbeat

    def set_last_heartbeat(self, time=None):
        time = time if time != None else time_int()
        if Config().is_cluster_enabled():
            self.cluster.session.set(Cluster.KEY_USER_LAST_HEARTBEAT, time)
        self.last_heartbeat = time

    # def init_cookies
    def is_first_time(self):
        if Config().is_cluster_enabled():
            return not self.cluster.get_user_cookie(self.key)
        return not path.exists(self.get_cookie_path())

    def handle_login(self, expire=False):
        if expire: UserLog.print_user_expired()
        self.is_ready = False
        UserLog.print_start_login(user=self)
        if self.type == 'qr':
            return self.qr_login()
        else:
            return self.login2()

    def login(self):
        """
        获取验证码结果
        :return 权限校验码
        """
        data = {
            'username': self.user_name,
            'password': self.password,
            'appid': 'otn'
        }
        answer = AuthCode.get_auth_code(self.session)
        data['answer'] = answer
        self.request_device_id()
        response = self.session.post(API_BASE_LOGIN.get('url'), data)
        result = response.json()
        if result.get('result_code') == 0:  # 登录成功
            """
            login 获得 cookie uamtk
            auth/uamtk      不请求，会返回 uamtk票据内容为空
            /otn/uamauthclient 能拿到用户名
            """
            new_tk = self.auth_uamtk()
            user_name = self.auth_uamauthclient(new_tk)
            self.update_user_info({'user_name': user_name})
            self.login_did_success()
            return True
        elif result.get('result_code') == 2:  # 账号之内错误
            # 登录失败，用户名或密码为空
            # 密码输入错误
            UserLog.add_quick_log(UserLog.MESSAGE_LOGIN_FAIL.format(result.get('result_message'))).flush()
        else:
            UserLog.add_quick_log(
                UserLog.MESSAGE_LOGIN_FAIL.format(result.get('result_message', result.get('message',
                                                                                          CommonLog.MESSAGE_RESPONSE_EMPTY_ERROR)))).flush()

        return False

    def qr_login(self):
        self.request_device_id()
        image_uuid, png_path = self.download_code()
        last_time = time_int()
        while True:
            data = {
                'RAIL_DEVICEID': self.session.cookies.get('RAIL_DEVICEID'),
                'RAIL_EXPIRATION': self.session.cookies.get('RAIL_EXPIRATION'),
                'uuid': image_uuid,
                'appid': 'otn'
            }
            response = self.session.post(API_AUTH_QRCODE_CHECK.get('url'), data)
            result = response.json()
            try:
                result_code = int(result.get('result_code'))
            except:
                if time_int() - last_time > 300:
                    last_time = time_int()
                    image_uuid, png_path = self.download_code()
                continue
            if result_code == 0:
                time.sleep(get_interval_num(self.sleep_interval))
            elif result_code == 1:
                UserLog.add_quick_log('请确认登录').flush()
                time.sleep(get_interval_num(self.sleep_interval))
            elif result_code == 2:
                break
            elif result_code == 3:
                try:
                    os.remove(png_path)
                except Exception as e:
                    UserLog.add_quick_log('无法删除文件: {}'.format(e)).flush()
                image_uuid, png_path = self.download_code()
            if time_int() - last_time > 300:
                last_time = time_int()
                image_uuid, png_path = self.download_code()
        try:
            os.remove(png_path)
        except Exception as e:
            UserLog.add_quick_log('无法删除文件: {}'.format(e)).flush()

        self.session.get(API_USER_LOGIN, allow_redirects=True)
        new_tk = self.auth_uamtk()
        user_name = self.auth_uamauthclient(new_tk)
        self.update_user_info({'user_name': user_name})
        self.session.get(API_USER_LOGIN, allow_redirects=True)
        self.login_did_success()
        return True

    def login2(self):
        data = {
            'username': self.user_name,
            'password': self.password,
            'user_card': self.user_card,
        }
        headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/94.0.4606.61 Safari/537.36",
                }
        self.session.headers.update(headers)
        cookies, post_data = Browser().request_init_slide2(self.session, data)
        while not cookies or not post_data:
            cookies, post_data = Browser().request_init_slide2(self.session, data)
        for cookie in cookies:
            self.session.cookies.update({
                   cookie['name']: cookie['value']
            })
        response = self.session.post(API_BASE_LOGIN.get('url')+ '?' + post_data)
        result = response.json()
        if result.get('result_code') == 0:  # 登录成功
            """
            login 获得 cookie uamtk
            auth/uamtk      不请求，会返回 uamtk票据内容为空
            /otn/uamauthclient 能拿到用户名
            """
            new_tk = self.auth_uamtk()
            user_name = self.auth_uamauthclient(new_tk)
            self.update_user_info({'user_name': user_name})
            self.login_did_success()
            return True
        elif result.get('result_code') == 2:  # 账号之内错误
            # 登录失败，用户名或密码为空
            # 密码输入错误
            UserLog.add_quick_log(UserLog.MESSAGE_LOGIN_FAIL.format(result.get('result_message'))).flush()
        else:
            UserLog.add_quick_log(
                UserLog.MESSAGE_LOGIN_FAIL.format(result.get('result_message', result.get('message',
                                                                                          CommonLog.MESSAGE_RESPONSE_EMPTY_ERROR)))).flush()

        return False

    def download_code(self):
        try:
            UserLog.add_quick_log(UserLog.MESSAGE_QRCODE_DOWNLOADING).flush()
            response = self.session.post(API_AUTH_QRCODE_BASE64_DOWNLOAD.get('url'), data={'appid': 'otn'})
            result = response.json()
            if result.get('result_code') == '0':
                img_bytes = base64.b64decode(result.get('image'))
                try:
                    os.mkdir(Config().USER_DATA_DIR + '/qrcode')
                except FileExistsError:
                    pass
                png_path = path.normpath(Config().USER_DATA_DIR + '/qrcode/%d.png' % time.time())
                with open(png_path, 'wb') as file:
                    file.write(img_bytes)
                    file.close()
                if os.name == 'nt':
                    os.startfile(png_path)
                else:
                    print_qrcode(png_path)
                UserLog.add_log(UserLog.MESSAGE_QRCODE_DOWNLOADED.format(png_path)).flush()
                Notification.send_email_with_qrcode(Config().EMAIL_RECEIVER, '你有新的登录二维码啦!', png_path)
                self.retry_count = 0
                return result.get('uuid'), png_path
            raise KeyError('获取二维码失败: {}'.format(result.get('result_message')))
        except Exception as e:
            sleep_time = get_interval_num(self.sleep_interval)
            UserLog.add_quick_log(
                UserLog.MESSAGE_QRCODE_FAIL.format(e, sleep_time)).flush()
            time.sleep(sleep_time)
            self.request_device_id(self.retry_count % 20 == 0)
            self.retry_count += 1
            return self.download_code()

    def check_user_is_login(self):
        retry = 0
        while retry < Config().REQUEST_MAX_RETRY:
            retry += 1
            response = self.session.get(API_USER_LOGIN_CHECK)
            is_login = response.json().get('data.is_login', False) == 'Y'
            if is_login:
                self.save_user()
                self.set_last_heartbeat()
                return self.get_user_info() # 检测应该是不会维持状态，这里再请求下个人中心看有没有用，01-10 看来应该是没用  01-22 有时拿到的状态 是已失效的再加上试试
            Browser().clear_iphone_number() # 检测到未登录需要清空手机验证码
            time.sleep(get_interval_num(self.sleep_interval))
        return is_login

    def auth_uamtk(self):
        retry = 0
        while retry < Config().REQUEST_MAX_RETRY:
            retry += 1
            response = self.session.post(API_AUTH_UAMTK.get('url'), {'appid': 'otn'}, headers={
                'Referer': 'https://kyfw.12306.cn/otn/passport?redirect=/otn/login/userLogin',
                'Origin': 'https://kyfw.12306.cn'
            })
            result = response.json()
            if result.get('newapptk'):
                return result.get('newapptk')
            # TODO 处理获取失败情况
        return False

    def auth_uamauthclient(self, tk):
        retry = 0
        while retry < Config().REQUEST_MAX_RETRY:
            retry += 1
            response = self.session.post(API_AUTH_UAMAUTHCLIENT.get('url'), {'tk': tk})
            result = response.json()
            if result.get('username'):
                return result.get('username')
            # TODO 处理获取失败情况
        return False

    def request_device_id(self, force_renew = False):
        """
        获取加密后的浏览器特征 ID
        :return:
        """
        # 判断cookie 是否过期，未过期可以不必下载
        expire_time =  self.session.cookies.get('RAIL_EXPIRATION')
        if not force_renew and expire_time and int(expire_time) - time_int_ms() > 0:
            return
        if 'pjialin' not in API_GET_BROWSER_DEVICE_ID:
            return self.request_device_id2()
        response = self.session.get(API_GET_BROWSER_DEVICE_ID)
        if response.status_code == 200:
            try:
                result = json.loads(response.text)
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/94.0.4606.61 Safari/537.36"
                }
                self.session.headers.update(headers)
                response = self.session.get(base64.b64decode(result['id']).decode())
                if response.text.find('callbackFunction') >= 0:
                    result = response.text[18:-2]
                result = json.loads(result)
                if not Config().is_cache_rail_id_enabled():
                   self.session.cookies.update({
                       'RAIL_EXPIRATION': result.get('exp'),
                       'RAIL_DEVICEID': result.get('dfp'),
                   })
                else:
                   self.session.cookies.update({
                       'RAIL_EXPIRATION': Config().RAIL_EXPIRATION,
                       'RAIL_DEVICEID': Config().RAIL_DEVICEID,
                   })
            except:
                return self.request_device_id()
        else:
            return self.request_device_id()

    def request_device_id2(self):
        headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/94.0.4606.61 Safari/537.36"
        }
        self.session.headers.update(headers)
        response = self.session.get(API_GET_BROWSER_DEVICE_ID)
        if response.status_code == 200:
            try:
                if response.text.find('callbackFunction') >= 0:
                    result = response.text[18:-2]
                    result = json.loads(result)
                    if not Config().is_cache_rail_id_enabled():
                       self.session.cookies.update({
                           'RAIL_EXPIRATION': result.get('exp'),
                           'RAIL_DEVICEID': result.get('dfp'),
                       })
                    else:
                       self.session.cookies.update({
                           'RAIL_EXPIRATION': Config().RAIL_EXPIRATION,
                           'RAIL_DEVICEID': Config().RAIL_DEVICEID,
                       })
            except:
                return self.request_device_id2()
        else:
            return self.request_device_id2()

    def login_did_success(self):
        """
        用户登录成功
        :return:
        """
        self.login_num += 1
        self.welcome_user()
        self.save_user()
        self.get_user_info()
        self.set_last_heartbeat()
        self.is_ready = True

    def welcome_user(self):
        UserLog.print_welcome_user(self)
        pass

    def get_cookie_path(self):
        return Config().USER_DATA_DIR + self.user_name + '.cookie'

    def update_user_info(self, info):
        self.info = {**self.info, **info}

    def get_name(self):
        return self.info.get('user_name', '')

    def save_user(self):
        if Config().is_master():
            self.cluster.set_user_cookie(self.key, self.session.cookies)
            self.cluster.set_user_info(self.key, self.info)
        with open(self.get_cookie_path(), 'wb') as f:
            pickle.dump(self.session.cookies, f)

    def did_loaded_user(self):
        """
        恢复用户成功
        :return:
        """
        UserLog.add_quick_log(UserLog.MESSAGE_LOADED_USER.format(self.user_name)).flush()
        if self.check_user_is_login() and self.can_access_passengers():
            UserLog.add_quick_log(UserLog.MESSAGE_LOADED_USER_SUCCESS.format(self.user_name)).flush()
            UserLog.print_welcome_user(self)
            self.user_did_load()
            return True
        else:
            UserLog.add_quick_log(UserLog.MESSAGE_LOADED_USER_BUT_EXPIRED).flush()
            self.set_last_heartbeat(0)
            return False

    def user_did_load(self):
        """
        用户已经加载成功
        :return:
        """
        self.is_ready = True
        if self.user_loaded: return
        self.user_loaded = True
        Event().user_loaded({'key': self.key})  # 发布通知

    def get_user_info(self):
        retry = 0
        while retry < Config().REQUEST_MAX_RETRY:
            retry += 1
            response = self.session.get(API_USER_INFO.get('url'))
            result = response.json()
            user_data = result.get('data.userDTO.loginUserDTO')
            # 子节点访问会导致主节点登录失效 TODO 可快考虑实时同步 cookie
            if user_data:
                self.update_user_info({**user_data, **{'user_name': user_data.get('name')}})
                self.save_user()
                return True
            time.sleep(get_interval_num(self.sleep_interval))
        return False

    def load_user(self):
        if Config().is_cluster_enabled(): return
        cookie_path = self.get_cookie_path()

        if path.exists(cookie_path):
            with open(self.get_cookie_path(), 'rb') as f:
                cookie = pickle.load(f)
                self.cookie = True
                self.session.cookies.update(cookie)
                return self.did_loaded_user()
        return None

    def load_user_from_remote(self):
        cookie = self.cluster.get_user_cookie(self.key)
        info = self.cluster.get_user_info(self.key)
        if Config().is_slave() and (not cookie or not info):
            while True:  # 子节点只能取
                UserLog.add_quick_log(UserLog.MESSAGE_USER_COOKIE_NOT_FOUND_FROM_REMOTE.format(self.user_name)).flush()
                stay_second(self.retry_time)
                return self.load_user_from_remote()
        if info: self.info = info
        if cookie:
            self.session.cookies.update(cookie)
            if not self.cookie:  # 第一次加载
                self.cookie = True
                if not Config().is_slave():
                    self.did_loaded_user()
                else:
                    self.is_ready = True  # 设置子节点用户 已准备好
                    UserLog.print_welcome_user(self)
            return True
        return False

    def check_is_ready(self):
        return self.is_ready

    def wait_for_ready(self):
        if self.is_ready: return self
        UserLog.add_quick_log(UserLog.MESSAGE_WAIT_USER_INIT_COMPLETE.format(self.retry_time)).flush()
        stay_second(self.retry_time)
        return self.wait_for_ready()

    def destroy(self):
        """
        退出用户
        :return:
        """
        UserLog.add_quick_log(UserLog.MESSAGE_USER_BEING_DESTROY.format(self.user_name)).flush()
        self.is_alive = False

    def response_login_check(self, response, **kwargs):
        if Config().is_master() and response.json().get('data.noLogin') == 'true':  # relogin
            self.handle_login(expire=True)

    def get_user_passengers(self):
        if self.passengers: return self.passengers
        response = self.session.post(API_USER_PASSENGERS)
        result = response.json()
        if result.get('data.normal_passengers'):
            self.passengers = result.get('data.normal_passengers')
            # 将乘客写入到文件
            with open(Config().USER_PASSENGERS_FILE % self.user_name, 'w', encoding='utf-8') as f:
                f.write(json.dumps(self.passengers, indent=4, ensure_ascii=False))
            return self.passengers
        else:
            wait_time = get_interval_num(self.sleep_interval)
            UserLog.add_quick_log(
                UserLog.MESSAGE_GET_USER_PASSENGERS_FAIL.format(
                    result.get('messages', CommonLog.MESSAGE_RESPONSE_EMPTY_ERROR), wait_time)).flush()
            if Config().is_slave():
                self.load_user_from_remote()  # 加载最新 cookie
            stay_second(wait_time)
            return self.get_user_passengers()

    def can_access_passengers(self):
        retry = 0
        while retry < Config().REQUEST_MAX_RETRY:
            retry += 1
            response = self.session.post(API_USER_PASSENGERS)
            result = response.json()
            if result.get('data.normal_passengers'):
                return True
            else:
                wait_time = get_interval_num(self.sleep_interval)
                UserLog.add_quick_log(
                    UserLog.MESSAGE_TEST_GET_USER_PASSENGERS_FAIL.format(
                        result.get('messages', CommonLog.MESSAGE_RESPONSE_EMPTY_ERROR), wait_time)).flush()
                if Config().is_slave():
                    self.load_user_from_remote()  # 加载最新 cookie
                stay_second(wait_time)
        return False

    def get_passengers_by_members(self, members):
        """
        获取格式化后的乘客信息
        :param members:
        :return:
        [{
            name: '项羽',
            type: 1,
            id_card: 0000000000000000000,
            type_text: '成人',
            enc_str: 'aaaaaa'
        }]
        """
        self.get_user_passengers()
        results = []
        for member in members:
            is_member_code = is_number(member)
            if not is_member_code:
                if member[0] == "*":
                    audlt = 1
                    member = member[1:]
                else:
                    audlt = 0
                child_check = array_dict_find_by_key_value(results, 'name', member)
            if not is_member_code and child_check:
                new_member = child_check.copy()
                new_member['type'] = UserType.CHILD
                new_member['type_text'] = dict_find_key_by_value(UserType.dicts, int(new_member['type']))
            else:
                if is_member_code:
                    passenger = array_dict_find_by_key_value(self.passengers, 'code', member)
                else:
                    passenger = array_dict_find_by_key_value(self.passengers, 'passenger_name', member)
                    if audlt:
                        passenger['passenger_type'] = UserType.ADULT
                if not passenger:
                    UserLog.add_quick_log(
                        UserLog.MESSAGE_USER_PASSENGERS_IS_INVALID.format(self.user_name, member)).flush()
                    return False
                new_member = {
                    'name': passenger.get('passenger_name'),
                    'id_card': passenger.get('passenger_id_no'),
                    'id_card_type': passenger.get('passenger_id_type_code'),
                    'mobile': passenger.get('mobile_no'),
                    'type': passenger.get('passenger_type'),
                    'type_text': dict_find_key_by_value(UserType.dicts, int(passenger.get('passenger_type'))),
                    'enc_str': passenger.get('allEncStr')
                }
            results.append(new_member)

        return results

    def request_init_dc_page(self):
        """
        请求下单页面 拿到 token
        :return:
        """
        data = {'_json_att': ''}
        response = self.session.post(API_INITDC_URL, data)
        html = response.text
        token = re.search(r'var globalRepeatSubmitToken = \'(.+?)\'', html)
        form = re.search(r'var ticketInfoForPassengerForm *= *(\{.+\})', html)
        order = re.search(r'var orderRequestDTO *= *(\{.+\})', html)
        # 系统忙，请稍后重试
        if html.find('系统忙，请稍后重试') != -1:
            OrderLog.add_quick_log(OrderLog.MESSAGE_REQUEST_INIT_DC_PAGE_FAIL).flush()  # 重试无用，直接跳过
            return False, False, html
        try:
            self.global_repeat_submit_token = token.groups()[0]
            self.ticket_info_for_passenger_form = json.loads(form.groups()[0].replace("'", '"'))
            self.order_request_dto = json.loads(order.groups()[0].replace("'", '"'))
        except:
            return False, False, html  # TODO Error

        slide_val = re.search(r"var if_check_slide_passcode.*='(\d?)'", html)
        is_slide = False
        if slide_val:
            is_slide = int(slide_val[1]) == 1
        return True, is_slide, html
