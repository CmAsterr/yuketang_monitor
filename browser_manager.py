import os
import sys
import re
import time
from selenium import webdriver
from selenium.webdriver.edge.service import Service
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    WebDriverException, NoAlertPresentException, 
    TimeoutException, InvalidSessionIdException,
    SessionNotCreatedException
)
import utils  # 导入通用工具模块

def init_browser():
    """初始化浏览器驱动并返回driver实例"""
    try:
        # 驱动路径（当前目录下的msedgedriver.exe）
        driver_filename = "msedgedriver.exe"

        if getattr(sys, 'frozen', False):
            # 打包后：获取.exe所在目录
            base_dir = os.path.dirname(sys.executable)
        else:
            # 未打包：使用原逻辑（脚本所在目录）
            base_dir = os.path.dirname(__file__)
        
        driver_path = os.path.join(base_dir, driver_filename)
        
        if not os.path.exists(driver_path):
            utils.log(f"[错误] 未找到驱动文件: {driver_path}")
            return None
        
        # 浏览器配置
        edge_options = Options()
        edge_options.add_argument("--start-maximized")  # 最大化窗口
        edge_options.add_argument("--no-sandbox")       # 禁用沙箱模式
        edge_options.add_argument("--disable-gpu")      # 禁用GPU加速
        edge_options.add_argument("--disable-popup-blocking")  # 禁用弹窗拦截
        edge_options.add_argument("--disable-blink-features=AutomationControlled")  # 隐藏自动化标识
        edge_options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        edge_options.add_experimental_option("detach", True)  # 浏览器不随脚本退出
        edge_options.add_experimental_option("prefs", {"profile.managed_default_content_settings.images": 1})  # 启用图片加载
        
        # 启动浏览器
        service = Service(executable_path=driver_path, log_path="logs/edge_driver.log", log_level=1)
        driver = webdriver.Edge(service=service, options=edge_options)
        utils.log("Edge浏览器启动成功（已隐藏自动化标识）")
        return driver
        
    except Exception as e:
        utils.log(f"[错误] 浏览器初始化失败: {str(e)}")
        return None

def check_edge_compatibility():
    """检查Edge浏览器和驱动是否兼容"""
    utils.log("开始检测Edge浏览器和驱动兼容性...")
    
    try:
        # 获取Edge浏览器版本
        edge_version = None
        if sys.platform.startswith("win"):
            possible_paths = [
                r"C:\Program Files (x86)\Microsoft\Edge\Application",
                r"C:\Program Files\Microsoft\Edge\Application",
                fr"C:\Users\{os.getlogin()}\AppData\Local\Microsoft\Edge\Application"
            ]
            
            for path in possible_paths:
                if os.path.exists(path):
                    version_dirs = [
                        d for d in os.listdir(path) 
                        if os.path.isdir(os.path.join(path, d)) and 
                        re.match(r'^\d+\.\d+\.\d+\.\d+$', d)
                    ]
                    
                    if version_dirs:
                        version_dirs.sort(key=lambda x: tuple(map(int, x.split('.'))), reverse=True)
                        edge_version = version_dirs[0]
                        utils.log(f"检测到Edge浏览器版本: {edge_version}")
                        break
            
            if not edge_version:
                utils.log("未找到Edge浏览器安装目录或版本信息（请确认Edge已安装）")
                return False
        else:
            utils.log("非Windows系统暂不支持自动版本检测（需手动确保版本匹配）")
            return True
            
        # 检查驱动是否存在
        # 判断是否为打包后的环境（_MEIPASS是pyinstaller的临时目录标识）
        if getattr(sys, 'frozen', False):
            # 打包后：获取.exe所在目录
            base_dir = os.path.dirname(sys.executable)
        else:
            # 未打包：使用原逻辑（脚本所在目录）
            base_dir = os.path.dirname(__file__)
        
        driver_path = os.path.join(base_dir, "msedgedriver.exe")
        if not os.path.exists(driver_path):
            utils.log(f"未找到驱动文件: {driver_path}（请下载对应版本驱动）")
            return False
            
        # 获取驱动版本
        try:
            service = Service(executable_path=driver_path)
            driver = webdriver.Edge(service=service)
            driver_version = driver.capabilities['browserVersion']
            driver.quit()
            utils.log(f"检测到Edge驱动版本: {driver_version}")
            
            # 比较主版本号
            edge_main_version = edge_version.split('.')[0]
            driver_main_version = driver_version.split('.')[0]
            
            if edge_main_version != driver_main_version:
                utils.log(f"版本不兼容: Edge主版本 {edge_main_version}, 驱动主版本 {driver_main_version}")
                utils.log("正在打开官网下载对应版本驱动...")
                download_driver = webdriver.Edge(service=Service(executable_path=driver_path))
                download_driver.get("https://developer.microsoft.com/zh-cn/microsoft-edge/tools/webdriver/")
                return False
            else:
                utils.log("Edge浏览器与驱动版本兼容（主版本号一致）")
                return True
                
        except Exception as e:
            utils.log(f"驱动版本检测失败: {str(e)}（可能是驱动损坏或路径错误）")
            utils.log("建议手动下载对应版本驱动: https://developer.microsoft.com/zh-cn/microsoft-edge/tools/webdriver/")
            return False
            
    except Exception as e:
        utils.log(f"兼容性检测异常: {str(e)}")
        return True

def handle_all_alerts(driver):
    """处理刷新确认和离开网站提示对话框"""
    try:
        if not driver:
            return False
            
        alert = WebDriverWait(driver, 3).until(EC.alert_is_present())
        alert_text = alert.text.strip().lower()
        if any(keyword in alert_text for keyword in ["离开此页", "放弃更改", "未保存"]):
            utils.log("检测到'离开页面'对话框，自动确认...")
        elif any(keyword in alert_text for keyword in ["刷新", "重新加载"]):
            utils.log("检测到'刷新确认'对话框，自动确认...")
        elif any(keyword in alert_text for keyword in ["确定", "确认"]):
            utils.log("检测到通用确认对话框，自动确认...")
        else:
            utils.log(f"检测到未知对话框（内容：{alert.text}），自动确认...")
        alert.accept()
        return True
    except TimeoutException:
        return False
    except NoAlertPresentException:
        utils.log("未检测到对话框（正常状态）")
        return False
    except InvalidSessionIdException:
        utils.log("处理对话框时发现会话已失效（需重新连接）")
        return False
    except Exception as e:
        utils.log(f"处理对话框异常: {str(e)}")
        return False

def extract_course_id(url):
    """从URL中提取课程号"""
    patterns = [
        r"lesson/fullscreen/v3/(\d+)",
        r"lesson/v3/(\d+)",
        r"v3/(\d+)/",
        r"course/(\d+)/lesson"
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            course_id = match.group(1)
            utils.log(f"从URL中提取到课程号: {course_id}（匹配格式：{pattern}）")
            return course_id
    
    utils.log(f"无法从URL中提取课程号（URL：{url[:50]}...）")
    return None

def get_active_tab_url(driver):
    """获取当前活动标签页的URL"""
    try:
        if not driver:
            utils.log("driver实例无效，无法获取URL")
            return None
            
        window_handles = driver.window_handles
        if not window_handles:
            utils.log("没有找到浏览器窗口（可能已关闭）")
            return None
            
        yuketang_domains = ["changjiang.yuketang.cn", "yuketang.cn", "www.yuketang.cn", "pro.yuketang.cn", "huanghe.yuketang.cn"]
        target_url = None
        
        for handle in window_handles:
            try:
                driver.switch_to.window(handle)
                current_url = driver.current_url
                if "about:blank" in current_url or "login" in current_url and "lesson" not in current_url:
                    continue
                if any(domain in current_url for domain in yuketang_domains) and "lesson" in current_url:
                    target_url = current_url
                    utils.log(f"在窗口 {handle} 找到雨课堂课程页面: {target_url}")
                    break
            except Exception as e:
                utils.log(f"切换窗口 {handle} 时出错: {str(e)}（跳过该窗口）")
                continue
        
        if not target_url:
            driver.switch_to.window(window_handles[-1])
            last_url = driver.current_url
            if "about:blank" not in last_url:
                utils.log(f"未找到明确的课程页面，返回最后一个有效窗口URL: {last_url}")
                return last_url
            else:
                utils.log("所有窗口均为空白页，无法获取有效URL")
                return None
        
        return target_url
        
    except InvalidSessionIdException:
        utils.log("获取URL时发现会话已失效（浏览器可能已关闭）")
        return None
    except Exception as e:
        utils.log(f"获取活动窗口URL失败: {str(e)}")
        return None

def reconnect_browser(course_url):
    """重新连接浏览器并导航到课程页面"""
    utils.log("开始尝试重新连接浏览器...")
    reconnect_delay = 3
    
    for attempt in range(3):
        utils.log(f"重连尝试 {attempt+1}/3（失败后等待{reconnect_delay}秒）")
        
        driver = init_browser()
        if not driver:
            utils.log(f"第{attempt+1}次重连失败：无法初始化浏览器")
            time.sleep(reconnect_delay)
            reconnect_delay *= 2
            continue
        
        try:
            utils.log(f"导航到课程页面: {course_url[:50]}...")
            driver.get(course_url)
            time.sleep(4)
            
            current_url = driver.current_url
            if "lesson" in current_url or "fullscreen" in current_url:
                utils.log("浏览器重连成功，已导航到课程页面")
                return driver
            elif "login" in current_url:
                utils.log("重连后需要登录，等待用户扫码...")
                input("[重新登录] 请在浏览器中完成扫码登录，登录后按回车键继续...")
                driver.get(course_url)
                time.sleep(3)
                if "lesson" in driver.current_url:
                    utils.log("用户登录完成，浏览器重连成功")
                    return driver
                else:
                    utils.log("用户登录后仍未进入课程页面")
            else:
                utils.log(f"重连后页面URL异常: {current_url}")
                
            driver.quit()
            utils.log(f"第{attempt+1}次重连失败：页面状态异常")
            time.sleep(reconnect_delay)
            reconnect_delay *= 2
            
        except Exception as e:
            utils.log(f"第{attempt+1}次重连过程中出错: {str(e)}")
            if driver:
                driver.quit()
            time.sleep(reconnect_delay)
            reconnect_delay *= 2
    
    utils.log("所有重连尝试均失败（共3次），建议检查浏览器和网络状态")
    return None

def locate_page_element(driver, page_type, xpath_config):
    """定位页面元素（PPT/选择题等）"""
    try:
        target_xpath = xpath_config.get(page_type)
        if not target_xpath:
            utils.log(f"[浏览器模块] 未找到{page_type}页面的XPath配置")
            return None
        
        utils.log(f"[浏览器模块] 定位{page_type}元素 (XPath: {target_xpath[:50]}...)")
        base_element = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, target_xpath))
        )
        utils.log(f"[浏览器模块] {page_type}元素定位成功")
        return base_element
    except Exception as e:
        utils.log(f"[浏览器模块] {page_type}元素定位异常: {str(e)}")
        return None
