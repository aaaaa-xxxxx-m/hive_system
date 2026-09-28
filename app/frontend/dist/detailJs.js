const deviceId = new URLSearchParams(window.location.search).get('id');
document.getElementById("deviceID").innerHTML = `设备名：${deviceId}`;


// ===================== 配置后端接口地址 (同源) =====================
const CONFIG = {
    // 温湿度/推理数据接口 (按设备名查询)
    DATA_API: `/api/v1/devices/by-name/${encodeURIComponent(deviceId)}`,

    // 模型更新接口
    MODEL_UPDATE_API: "/api/v1/models/train",
};

let if_upgrade = localStorage.getItem('is_upgrading') === 'true';

// ===================== 1. 温湿度获取 =====================
function getSensorData() {
    fetch(CONFIG.DATA_API, {
        method:'GET'
    })
    .then(res => {
	if (!res.ok) throw new Error(`HTTP错误: ${res.status}`);
        return res.json();
    })
    .then(data => {
        // 更新相关数据
        const temp = parseFloat(data.temperature);
        const humid = parseFloat(data.humidity);
        const queen_prob = parseFloat(data.probability_no_queen);
        document.getElementById("temp").innerText = isNaN(temp) ? "暂无数据" : temp.toFixed(1);
        document.getElementById("humi").innerText = isNaN(humid) ? "暂无数据" : humid.toFixed(1);

        if(!isNaN(queen_prob)){
            if(queen_prob >= 0 && queen_prob < 0.2) document.getElementById("queen_result").innerText = `蜂群状态正常`;
            else if(queen_prob >= 0.2 && queen_prob < 0.4) document.getElementById("queen_result").innerText = `蜂群状态发生轻微波动`;
            else if(queen_prob >= 0.4 && queen_prob < 0.6) document.getElementById("queen_result").innerText = `蜂群状态发生较明显波动，蜂群发生失王概率为 ${queen_prob.toFixed(4)*100}%`;
            else if(queen_prob >= 0.6 && queen_prob < 0.8) document.getElementById("queen_result").innerText = `蜂群状态发生显著波动，蜂群发生失王概率为 ${queen_prob.toFixed(4)*100}%`;
            else if(queen_prob >=0.8 && queen_prob <=1) document.getElementById("queen_result").innerText = `蜂群已失王，请立即查看蜂箱情况`;
        }else{
            document.getElementById("queen_result").innerText = `暂无推理数据`;
        }

        // 更新时间
        let now = new Date().toLocaleString();
        document.getElementById("temp_time").innerText = "更新：" + now;
        document.getElementById("humi_time").innerText = "更新：" + now;

        // 环境状态判断
        let envStatus = "正常";
        let statusColor = "#2E7D32";
        let tip = "适合蜂群生存";
        if (isNaN(temp) && isNaN(humid)) {
            envStatus = "离线";
            statusColor = "#c62828";
            tip = "无传感器数据";
        } else if (temp < 20 || temp > 30) {
            envStatus = "异常";
            statusColor = "#c62828";
            tip = "温度不适，需调整";
        } else if (humid < 50 || humid > 75) {
            envStatus = "警告";
            statusColor = "#F57C00";
            tip = "湿度偏高/偏低";
        }

        document.getElementById("env_status").innerText = envStatus;
        document.getElementById("env_status").style.color = statusColor;
        document.getElementById("env_tip").innerText = tip;
    })
    .catch(err => {
        console.error("温湿度获取失败：", err);
        document.getElementById("temp").innerText = "离线";
        document.getElementById("humi").innerText = "离线";
        document.getElementById("env_status").innerText = "离线";
        document.getElementById("env_status").style.color = "#c62828";
        document.getElementById("env_tip").innerText = "无法连接传感器";
    });
}

function wait_upgrade() {
    fetch(CONFIG.DATA_API, {
        method:'GET'
    })
    .then(res => {
        if (!res.ok) throw new Error(`HTTP错误: ${res.status}`);
        return res.json();
    })
    .then(data => {
        const temp = parseFloat(data.temperature);
        const humid = parseFloat(data.humidity);
	if (data.success) {
	    page_load();
	    if_upgrade = false;
	    localStorage.setItem('is_upgrading', 'false');
	    return;
	}
        document.getElementById("temp").innerText = isNaN(temp) ? "暂无数据" : temp.toFixed(1);
        document.getElementById("humi").innerText = isNaN(humid) ? "暂无数据" : humid.toFixed(1);

	document.getElementById("queen_result").innerText = `模型正在更新中，请稍后`;

        // 更新时间
        let now = new Date().toLocaleString();
        document.getElementById("temp_time").innerText = "更新：" + now;
        document.getElementById("humi_time").innerText = "更新：" + now;
    })
}

// ===================监测按钮情况====================
document.getElementById("updateModelBtn").onclick = async function () {
    if(!if_upgrade){  //前端没有催动更新，这时按下按钮表明需要催动更新
        let btn = document.getElementById("updateModelBtn");
        let statusBox = document.getElementById("modelUpdateStatus");
        let statusContent = document.getElementById("updateStatusContent");

        if (!confirm("确定要更新检测模型吗？\n更新过程中检测功能将暂时不可用！")) {  //取消更新
            return;
        }else{  //确定更新
            // 显示状态框
            statusBox.classList.add("active");
            statusContent.innerHTML = "🔄 开始模型更新";
	    document.getElementById("queen_result").innerText = `模型正在更新中，请稍后`;
            if_upgrade = true;  //表示已经开始催动更新
	    localStorage.setItem('is_upgrading', true);
            alert("模型开始更新，请稍后")
            return;
        }
    }
    else{  //前端已经催动更新了
        alert("模型正在更新中");
        return;
    }
};

// =================模型更新成功，更新页面信息==================
function page_load(){
    let statusContent = document.getElementById("updateStatusContent");
    let statusBox = document.getElementById("modelUpdateStatus");
    statusContent.innerHTML = `模型更新成功！`;
    // 3秒后自动隐藏状态框
    setTimeout(() => {
        statusBox.classList.remove("active");
    }, 3000);
}
     

// ===================== 初始化 =====================
// 每5秒刷新温湿度
function pollData(){
    if(!if_upgrade){
        getSensorData();
    }
    else{
	document.getElementById("modelUpdateStatus").classList.add("active");
        document.getElementById("updateStatusContent").innerHTML = "🔄 模型更新中";
        wait_upgrade();
    }
}

pollData();
setInterval(pollData, 6000);
