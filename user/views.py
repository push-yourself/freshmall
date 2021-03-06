from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import check_password
from django.core.mail import send_mail
from django.http import HttpResponse, HttpRequest
from django.shortcuts import render, redirect
import re
# Create your views here.
from django.urls import reverse
from django.views.generic.base import View
from django_redis import get_redis_connection
from itsdangerous import TimedJSONWebSignatureSerializer as Serializer, SignatureExpired

from freshmall.settings import SECRET_KEY, EMAIL_FROM
from goods.models import GoodsSKU
from user.models import User, Address
from utils.mixin import LoginRequiredMixin
from user import task

class RegisterView(View):
    def get(self, request: HttpRequest):
        '''注册页面处理'''
        return render(request, 'register.html')

    def post(self, request):
        # 1.接收数据
        username = request.POST.get('user_name')
        password = request.POST.get('pwd')
        email = request.POST.get('email')
        allow = request.POST.get('allow')
        # 2.数据校验
        if not all([username, password, email]):
            # 提交数据不完整
            return render(request, 'register.html', {'errmsg': '数据不完整'})
        # 校验邮箱
        if not re.match(r'^[a-z0-9][\w\.\-]*@[a-z0-9\-]+(\.[a-z]{2,5}){1,2}$', email):
            return render(request, 'register.html', {'errmsg': '邮箱格式不合法'})
        # 校验密码是否一致
        if allow != 'on':
            return render(request, 'register.html', {'errmsg': '请同意协议'})
        # 校验用户名是否重复
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            # 用户名不存在
            user = None
        if user:
            # 用户存在的处理
            return render(request, 'register.html', {'errmsg': '用户名已存在'})
        # 3.进行业务处理：用户注册(Django中的用户表可以直接使用)
        user = User.objects.create_user(username, email, password)
        user.is_active = 0
        user.save()
        # 附加功能：发送激活邮件，包含激活链接(激活链接中需要包含用户的身份信息)
        # 采用自定义标识：http://127.0.0.1:8000/user/active/uid   注意：对用户身份信息进行加密，使用itsdangerous
        # 1.加密用户的身份信息，生成激活token
        serializer = Serializer(SECRET_KEY, 3600)
        info = {'confirm': user.id}  # 根据情况去自定义数据类型
        token = serializer.dumps(info).decode()
        # 2.发送邮件
        # 发邮件[通过异步实现]
        task.send_register_active_email.delay(email, username, token)
        # 4.返回应答(注册成功之后，跳转至首页)
        return redirect(reverse('goods:index'))


class ActiveView(View):
    '''用户激活'''

    def get(self, request, token):
        '''进行用户激活解密操作'''
        serializer = Serializer(SECRET_KEY, 3600)
        try:
            info = serializer.loads(token)
            # 获取用户ID
            user_id = info['confirm']
            user = User.objects.get(id=user_id)
            user.is_active = 1  # 修改激活状态
            user.save()
            # 返回应答,跳转至登录页面
            return redirect(reverse('user:login'))

        except SignatureExpired as e:
            # 激活链接过期
            return HttpResponse('激活链接已过期')


class LoginView(View):
    '''登录'''

    def get(self, request):
        # 判断是否记住了用户名
        if 'username' in request.COOKIES:
            username = request.COOKIES.get('username')
            checked = 'checked'
        else:
            username = ''
            checked = ''
        return render(request, 'login.html', {'username': username, 'checked': checked})

    def post(self, request):
        '''登录校验'''
        # 1.接收数据
        username = request.POST.get('username')
        password = request.POST.get('pwd')
        # 2. 校验
        if not all([username, password]):
            return render(request, 'login.html', {'errmsg': '数据不完整'})
        # 3.业务处理
        user = authenticate(request, username=username, password=password)
        if user is not None:
            if user.is_active:
                try:
                    login(request, user)
                except Exception as e:
                    print('**：', e)
                # 获取登录后需要跳转的地址:http://127.0.0.1:8000/user/login/?next=/user/
                # 如果没有,默认跳转至首页
                next_url = request.GET.get('next', reverse('goods:index'))
                print(next_url)
                response = redirect(next_url)
                # 判断是否需要记住用户名
                remember = request.POST.get('remember')
                if remember == 'on':
                    response.set_cookie('username', username, max_age=7 * 24 * 3600)
                else:
                    response.delete_cookie('username')
                return response

            else:
                return render(request, 'login.html', {'errmsg': '账户未激活'})
        else:
            return render(request, 'login.html', {'errmsg': '密码错误'})


# /user/logout
class LogoutView(View):
    '''退出登录'''

    def get(self, request):
        # 清楚用户的session信息
        logout(request)
        # 跳转首页
        return redirect(reverse('goods:index'))


# /user
class UserInfoView(LoginRequiredMixin, View):
    '''用户中心'''

    def get(self, request):
        '''显示'''
        # Django会给request对象添加一个属性,request.user
        # 如果用户未登录--->user为AnoymousUser类的一个实例
        # 如果用户登录---> user为User类的一个实例

        # 获取用户的个人信息
        user = request.user
        addr = Address.objects.get_default_address(user=user)

        # TODO:获取用户的历史记录
        con = get_redis_connection('default')
        history_key = 'history_%d' % user.id

        # 获取用户最新浏览的5个商品的具体信息
        sku_id =  con.lrange(history_key,0,4)
        # 根据sku_id从数据库中去查询记录
        # goods_li = GoodsSKU.objects.filter(id__in=sku_id)

        # 遍历获取用户浏览的商品信息(需要保证用户的历史浏览顺序)
        goods_li = []
        for id in sku_id:
            goods = GoodsSKU.objects.get(id=id)
            goods_li.append(goods)

        # 组织上下文
        context = {
            'page':'user',
            'address':addr,
            'goods_li':goods_li
        }
        # 除了给模板文件传递变量,django也会将request.user也传给模板文件
        return render(request, 'user_center_info.html', context)


# /user/address
class AddressView(LoginRequiredMixin, View):
    '''用户地址中心'''

    def get(self, request):
        '''显示'''
        # 获取用户的默认收获地址
        user = request.user
        # try:
        #     address = Address.objects.get(user = user,is_default = True)
        # except Address.DoesNotExist:
        #     # 则不存在默认地址
        #     address = None
        address = Address.objects.get_default_address(user=user)
        print(address)
        # 将地址返回进行判断
        return render(request, 'user_center_site.html', {'page': 'address', 'address': address})

    def post(self, request):
        '''添加地址'''
        # 1. 接收数据
        receiver = request.POST.get('receiver')
        addr = request.POST.get('addr')
        zip_code = request.POST.get('zip_code')
        phone = request.POST.get('phone')
        # 2. 校验数据
        if not all([receiver, addr, phone]):
            return render(request, 'user_center_site.html', {'errmsg': '信息不完整'})
            # 校验手机号
        if not re.match(r'^1[3|4|5|7|8|][0-9]{9}$', phone):
            return render(request, 'user_center_site.html', {'errmsg': '手机格式不正确'})
        # 3. 处理
        # 处理操作:
        # 已存在默认收获地址,则添加的地址不作为默认收获地址;否则作为默认收获地址
        user = request.user
        # try:
        #     address = Address.objects.get(user = user,is_default = True)
        # except Address.DoesNotExist:
        #     address = None
        # 则不存在默认地址
        address = Address.objects.get_default_address(user=user)
        if address:
            is_default = False
        else:
            is_default = True

        # 添加地址
        Address.objects.create(
            user=user,
            receiver=receiver,
            addr=addr,
            zip_code=zip_code,
            phone=phone,
            is_default=is_default
        )

        return render(request, 'user_center_site.html', {'page': 'address', 'address': address})


# /user/order
class UserOrderView(LoginRequiredMixin, View):
    '''用户订购中心'''

    def get(self, request):
        '''显示'''

        # TODO:获取用户的订单信息

        return render(request, 'user_center_order.html', {'page': 'order'})
