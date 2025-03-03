# -*- coding: utf-8 -*-

import logging
from datetime import datetime, timedelta
from os import getenv
from os.path import join, abspath, dirname

import httpx
from flask import Flask, jsonify, request, render_template, redirect, url_for, make_response
from pandora.exts.hooks import hook_logging
from pandora.exts.token import check_access_token, get_token_by_name
from pandora.openai.auth import Auth0
from waitress import serve
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.serving import WSGIRequestHandler

from . import __version__


class ChatBot:
    __default_ip = '127.0.0.1'
    __default_port = 8018
    __build_id = 'cx416mT2Lb0ZTj5FxFg1l'

    def __init__(self, proxy, debug=False, sentry=False, login_local=False):
        self.proxy = proxy
        self.debug = debug
        self.sentry = sentry
        self.login_local = login_local
        self.log_level = logging.DEBUG if debug else logging.WARN

        hook_logging(level=self.log_level, format='[%(asctime)s] %(levelname)s in %(module)s: %(message)s')
        self.logger = logging.getLogger('waitress')

    def run(self, bind_str, threads=8, listen=True):
        host, port = self.__parse_bind(bind_str)

        resource_path = abspath(join(dirname(__file__), 'flask'))
        app = Flask(__name__, static_url_path='',
                    static_folder=join(resource_path, 'static'),
                    template_folder=join(resource_path, 'templates'))
        app.wsgi_app = ProxyFix(app.wsgi_app, x_port=1)
        app.after_request(self.__after_request)

        app.route('/api/auth/session')(self.session)
        app.route('/api/accounts/check/v4-2023-04-27')(self.check)
        app.route('/auth/logout')(self.logout)
        app.route('/error/404')(self.error404)
        app.route('/_next/data/{}/index.json'.format(self.__build_id))(self.chat_info)
        app.route('/_next/data/{}/c/<conversation_id>.json'.format(self.__build_id))(self.chat_info)
        app.route('/_next/data/{}/share/<share_id>.json'.format(self.__build_id))(self.share_info)
        app.route('/_next/data/{}/share/<share_id>/continue.json'.format(self.__build_id))(self.share_continue_info)

        app.route('/')(self.chat)
        app.route('/c')(self.chat)

        app.route('/c/<conversation_id>')(self.chat)
        app.route('/share/<share_id>')(self.share_detail)
        app.route('/share/<share_id>/continue')(self.share_continue)

        app.route('/chat')(self.chat_index)
        app.route('/chat/<conversation_id>')(self.chat_index)

        app.route('/auth/login')(self.login)
        app.route('/auth/login_share')(self.login_share_token)
        app.route('/auth/login', methods=['POST'])(self.login_post)
        app.route('/auth/login_token', methods=['POST'])(self.login_token)
        app.route('/auth/login_name', methods=['POST'])(self.login_name)

        if not self.debug:
            self.logger.warning('Serving on http://{}:{}'.format(host, port))

        WSGIRequestHandler.protocol_version = 'HTTP/1.1'
        if listen:
            serve(app, host=host, port=port, ident=None, threads=threads)

        return app

    @staticmethod
    def __after_request(resp):
        resp.headers['X-Server'] = 'pandora-cloud/{}'.format(__version__)
        return resp

    def __parse_bind(self, bind_str):
        sections = bind_str.split(':', 2)
        if len(sections) < 2:
            try:
                port = int(sections[0])
                return self.__default_ip, port
            except ValueError:
                return sections[0], self.__default_port

        return sections[0], int(sections[1])

    @staticmethod
    def __get_api_prefix():
        default = 'https://ai-{}.fakeopen.com'.format((datetime.now() - timedelta(days=1)).strftime('%Y%m%d'))
        return getenv('CHATGPT_API_PREFIX', default)

    @staticmethod
    def __set_cookie(resp, token, expires):
        resp.set_cookie('access-token', token, expires=expires, path='/', domain=None, httponly=True, samesite='Lax')

    async def __get_userinfo(self):
        access_token = request.cookies.get('access-token')
        try:
            payload = check_access_token(access_token)
            if True == payload:
                ti = await self.__fetch_share_tokeninfo(access_token)
                return False, ti['user_id'], ti['email'], access_token, {'exp': ti['expire_at']}

            if 'https://api.openai.com/auth' not in payload or 'https://api.openai.com/profile' not in payload:
                raise Exception('invalid access token')
        except:
            return True, None, None, None, None

        user_id = payload['https://api.openai.com/auth']['user_id']
        email = payload['https://api.openai.com/profile']['email']

        return False, user_id, email, access_token, payload

    async def __fetch_share_tokeninfo(self, share_token):
        url = self.__get_api_prefix() + '/token/info/{}'.format(share_token)

        async with httpx.AsyncClient(proxies=self.proxy, timeout=30) as client:
            response = await client.get(url)
            if response.status_code == 404:
                raise Exception('share token not found or expired')

            if response.status_code != 200:
                raise Exception('failed to fetch share token info')

            return response.json()

    async def __fetch_share_detail(self, share_id):
        url = self.__get_api_prefix() + '/api/share/{}'.format(share_id)

        async with httpx.AsyncClient(proxies=self.proxy, timeout=30) as client:
            response = await client.get(url)
            if response.status_code != 200:
                raise Exception('failed to fetch share detail')

            return response.json()

    @staticmethod
    async def chat_index(conversation_id=None):
        resp = redirect('/')

        return resp

    async def logout(self):
        resp = redirect(url_for('login'))
        self.__set_cookie(resp, '', 0)

        return resp

    async def login(self):
        return render_template('login.html', api_prefix=self.__get_api_prefix(), next=request.args.get('next', ''))

    async def login_post(self):
        username = request.form.get('username')
        password = request.form.get('password')
        mfa_code = request.form.get('mfa_code')
        next_url = request.form.get('next')
        error = None

        if username and password:
            try:
                access_token = Auth0(username, password, self.proxy, mfa=mfa_code).auth(self.login_local)
                payload = check_access_token(access_token)

                resp = make_response('please wait...', 302)
                resp.headers.set('Location', next_url if next_url else '/')
                self.__set_cookie(resp, access_token, payload['exp'])

                return resp
            except Exception as e:
                error = str(e)

        return render_template('login.html', username=username, error=error, api_prefix=self.__get_api_prefix())

    async def login_share_token(self):
        share_token = request.args.get('token')
        error = None

        if share_token and share_token.startswith('fk-'):
            try:
                ti = await self.__fetch_share_tokeninfo(share_token)
                payload = {'exp': ti['expire_at']}

                resp = make_response('please wait...', 307)
                resp.headers.set('Location', '/')
                self.__set_cookie(resp, share_token, payload['exp'])

                return resp
            except Exception as e:
                error = str(e)

        return render_template('login.html', error=error, api_prefix=self.__get_api_prefix())

    async def login_token(self):
        access_token = request.form.get('access_token')
        next_url = request.form.get('next')
        error = None

        if access_token:
            try:
                payload = check_access_token(access_token)
                if True == payload:
                    ti = await self.__fetch_share_tokeninfo(access_token)
                    payload = {'exp': ti['expire_at']}

                resp = jsonify({'code': 0, 'url': next_url if next_url else '/'})
                self.__set_cookie(resp, access_token, payload['exp'])

                return resp
            except Exception as e:
                error = str(e)

        return jsonify({'code': 500, 'message': 'Invalid access token: {}'.format(error)})


    async def login_name(self):
        login_name = request.form.get('login_name')
        next_url = request.form.get('next')
        error = None

        if login_name:
            try:
                access_token = get_token_by_name(login_name)
                if access_token is None:
                    raise Exception('cannot find your account')

                payload = check_access_token(access_token)
                if True == payload:
                    ti = await self.__fetch_share_tokeninfo(access_token)
                    payload = {'exp': ti['expire_at']}

                resp = jsonify({'code': 0, 'url': next_url if next_url else '/'})
                self.__set_cookie(resp, access_token, payload['exp'])

                return resp
            except Exception as e:
                error = str(e)

        return jsonify({'code': 500, 'message': 'Invalid name: {}'.format(error)})
    

    async def chat(self, conversation_id=None):
        err, user_id, email, _, _ = await self.__get_userinfo()
        if err:
            return redirect(url_for('login'))

        query = request.args.to_dict()
        if conversation_id:
            query['chatId'] = conversation_id

        props = {
            'props': {
                'pageProps': {
                    'user': {
                        'id': user_id,
                        'name': email,
                        'email': email,
                        'image': None,
                        'picture': None,
                        'groups': [],
                    },
                    'serviceStatus': {},
                    'userCountry': 'US',
                    'geoOk': True,
                    'serviceAnnouncement': {
                        'paid': {},
                        'public': {}
                    },
                    'isUserInCanPayGroup': True
                },
                '__N_SSP': True
            },
            'page': '/c/[chatId]' if conversation_id else '/',
            'query': query,
            'buildId': self.__build_id,
            'isFallback': False,
            'gssp': True,
            'scriptLoader': []
        }

        template = 'detail.html' if conversation_id else 'chat.html'
        return render_template(template, pandora_sentry=self.sentry, api_prefix=self.__get_api_prefix(), props=props)

    async def session(self):
        err, user_id, email, access_token, payload = await self.__get_userinfo()
        if err:
            return jsonify({})

        ret = {
            'user': {
                'id': user_id,
                'name': email,
                'email': email,
                'image': None,
                'picture': None,
                'groups': [],
            },
            'expires': datetime.utcfromtimestamp(payload['exp']).isoformat(),
            'accessToken': access_token,
            'authProvider': 'auth0',
        }

        return jsonify(ret)

    async def error404(self):
        props = {
            'props': {
                'pageProps': {'statusCode': 404}
            },
            'page': '/_error',
            'query': {},
            'buildId': self.__build_id,
            'nextExport': True,
            'isFallback': False,
            'gip': True,
            'scriptLoader': []
        }
        return render_template('404.html', pandora_sentry=self.sentry, api_prefix=self.__get_api_prefix(), props=props)

    async def share_detail(self, share_id):
        err, user_id, email, _, _ = await self.__get_userinfo()
        if err:
            return redirect('/auth/login?next=%2Fshare%2F{}'.format(share_id))

        try:
            share_detail = await self.__fetch_share_detail(share_id)
        except:
            props = {
                'props': {
                    'pageProps': {'statusCode': 404}
                },
                'page': '/_error',
                'query': {},
                'buildId': self.__build_id,
                'nextExport': True,
                'isFallback': False,
                'gip': True,
                'scriptLoader': []
            }
            return render_template('404.html', pandora_sentry=self.sentry, api_prefix=self.__get_api_prefix(),
                                   props=props)

        if 'continue_conversation_url' in share_detail:
            share_detail['continue_conversation_url'] = share_detail['continue_conversation_url'].replace(
                'https://chat.openai.com', '')

        props = {
            'props': {
                'pageProps': {
                    'sharedConversationId': share_id,
                    'serverResponse': {
                        'type': 'data',
                        'data': share_detail
                    },
                    'continueMode': False,
                    'moderationMode': False,
                    'chatPageProps': {},
                },
                '__N_SSP': True
            },
            'page': '/share/[[...shareParams]]',
            'query': {
                'shareParams': [share_id]
            },
            'buildId': self.__build_id,
            'isFallback': False,
            'gssp': True,
            'scriptLoader': []
        }

        return render_template('share.html', pandora_sentry=self.sentry, api_prefix=self.__get_api_prefix(),
                               props=props)

    @staticmethod
    async def share_continue(share_id):
        return redirect('/share/{}'.format(share_id), code=308)

    async def share_info(self, share_id):
        try:
            share_detail = await self.__fetch_share_detail(share_id)
        except:
            return jsonify({'notFound': True})

        if 'continue_conversation_url' in share_detail:
            share_detail['continue_conversation_url'] = share_detail['continue_conversation_url'].replace(
                'https://chat.openai.com', '')

        props = {
            'pageProps': {
                'sharedConversationId': share_id,
                'serverResponse': {
                    'type': 'data',
                    'data': share_detail,
                },
                'continueMode': False,
                'moderationMode': False,
                'chatPageProps': {},
            },
            '__N_SSP': True
        }

        return jsonify(props)

    async def share_continue_info(self, share_id):
        err, user_id, email, _, _ = await self.__get_userinfo()
        if err:
            return jsonify({
                'pageProps': {
                    '__N_REDIRECT': '/auth/login?next=%2Fshare%2F{}%2Fcontinue'.format(share_id),
                    '__N_REDIRECT_STATUS': 307
                },
                '__N_SSP': True
            })

        share_detail = await self.__fetch_share_detail(share_id)
        if 'continue_conversation_url' in share_detail:
            share_detail['continue_conversation_url'] = share_detail['continue_conversation_url'].replace(
                'https://chat.openai.com', '')

        props = {
            'pageProps': {
                'user': {
                    'id': user_id,
                    'name': email,
                    'email': email,
                    'image': None,
                    'picture': None,
                    'groups': [],
                },
                'serviceStatus': {},
                'userCountry': 'US',
                'geoOk': True,
                'serviceAnnouncement': {
                    'paid': {},
                    'public': {}
                },
                'isUserInCanPayGroup': True,
                'sharedConversationId': share_id,
                'serverResponse': {
                    'type': 'data',
                    'data': share_detail,
                },
                'continueMode': True,
                'moderationMode': False,
                'chatPageProps': {
                    'user': {
                        'id': user_id,
                        'name': email,
                        'email': email,
                        'image': None,
                        'picture': None,
                        'groups': [],
                    },
                    'serviceStatus': {},
                    'userCountry': 'US',
                    'geoOk': True,
                    'serviceAnnouncement': {
                        'paid': {},
                        'public': {}
                    },
                    'isUserInCanPayGroup': True,
                },
            },
            '__N_SSP': True
        }

        return jsonify(props)

    async def chat_info(self, conversation_id=None):
        err, user_id, email, _, _ = await self.__get_userinfo()
        if err:
            return jsonify({'pageProps': {'__N_REDIRECT': '/auth/login?', '__N_REDIRECT_STATUS': 307}, '__N_SSP': True})

        ret = {
            'pageProps': {
                'user': {
                    'id': user_id,
                    'name': email,
                    'email': email,
                    'image': None,
                    'picture': None,
                    'groups': [],
                },
                'serviceStatus': {},
                'userCountry': 'US',
                'geoOk': True,
                'serviceAnnouncement': {
                    'paid': {},
                    'public': {}
                },
                'isUserInCanPayGroup': True
            },
            '__N_SSP': True
        }

        return jsonify(ret)

    @staticmethod
    async def check():
        ret = {
            'accounts': {
                'default': {
                    'account': {
                        'account_user_role': 'account-owner',
                        'account_user_id': 'd0322341-7ace-4484-b3f7-89b03e82b927',
                        'processor': {
                            'a001': {
                                'has_customer_object': True
                            },
                            'b001': {
                                'has_transaction_history': True
                            }
                        },
                        'account_id': 'a323bd05-db25-4e8f-9173-2f0c228cc8fa',
                        'is_most_recent_expired_subscription_gratis': True,
                        'has_previously_paid_subscription': True
                    },
                    'features': [
                        'model_switcher',
                        'model_preview',
                        'system_message',
                        'data_controls_enabled',
                        'data_export_enabled',
                        'show_existing_user_age_confirmation_modal',
                        'bucketed_history',
                        'priority_driven_models_list',
                        'message_style_202305',
                        'layout_may_2023',
                        'plugins_available',
                        'beta_features',
                        'infinite_scroll_history',
                        'browsing_available',
                        'browsing_inner_monologue',
                        'browsing_bing_branding',
                        'shareable_links',
                        'plugin_display_params',
                        'tools3_dev',
                        'tools2',
                        'debug',
                    ],
                    'entitlement': {
                        'subscription_id': 'd0dcb1fc-56aa-4cd9-90ef-37f1e03576d3',
                        'has_active_subscription': True,
                        'subscription_plan': 'chatgptplusplan',
                        'expires_at': '2089-08-08T23:59:59+00:00'
                    },
                    'last_active_subscription': {
                        'subscription_id': 'd0dcb1fc-56aa-4cd9-90ef-37f1e03576d3',
                        'purchase_origin_platform': 'chatgpt_mobile_ios',
                        'will_renew': True
                    }
                }
            },
            'temp_ap_available_at': '2023-05-20T17:30:00+00:00'
        }

        return jsonify(ret)
