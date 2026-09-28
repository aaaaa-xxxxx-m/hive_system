document.addEventListener('DOMContentLoaded', function() {
            const addDeviceBtn = document.getElementById('addDeviceBtn');
            const deviceModal = document.getElementById('deviceModal');
            const deviceForm = document.getElementById('deviceForm');
            const deviceList = document.getElementById('deviceList');
            const deviceNameInput = document.getElementById('deviceName');
            const btnClose = document.querySelector('.btn-close');

            fetch('/api/v1/devices/summary', {
                method: 'GET'
            })
            .then(response => {
                return response.json();
            })
            .then(deviceDataList => {
                if (Array.isArray(deviceDataList) && deviceDataList.length > 0) {
                deviceDataList.forEach((item) => {
                    const deviceName = item.device_name;
                    const temperature = item.temperature;
                    const humidity = item.humidity;
                    const deviceDiv = createDeviceElement(deviceName, temperature, humidity);
                    deviceList.appendChild(deviceDiv);
            });
        }
            })
            .catch(error => {
                console.error('请求出错:', error);
            });
            setInterval(refreshData, 6000);
            function createDeviceElement(deviceName, temperature, humidity) {
                const div = document.createElement('div');
                div.className = `deviceItem`;
                div.id = deviceName;

                div.addEventListener('click', function() {
                    window.location.href = `detail.html?id=${encodeURIComponent(deviceName)}`;
                });
               
                div.innerHTML = `
                    <h3>${deviceName}</h3>
                    <p>箱内温度:${temperature === null || temperature === undefined ? '--' : temperature}</p>
                    <p>箱内湿度:${humidity === null || humidity === undefined ? '--' : humidity}</p>
                    <button class="delete-btn">删除设备</button>
                `;
                const btn = div.querySelector('.delete-btn');
                btn.addEventListener('click', function(e) {
                    e.stopPropagation();

                    fetch(`/api/v1/devices/by-name/${encodeURIComponent(deviceName)}`, {
                        method: 'GET'
                    })
                    .then(res => res.json())
                    .then(device => fetch(`/api/v1/devices/${device.id}`, {
                        method: 'DELETE'
                    }))
                    .then(res => {
                        if (res.ok) {
                            alert('设备删除成功！');
                            div.remove();
                        } else {
                            alert('设备删除失败！');
                        }
                    })
                    .catch(error => {
                        console.error('删除设备出错:', error);
                        alert('设备删除失败！');
                    });
                })
                return div;
            }
            function refreshData(){
                fetch('/api/v1/devices/summary', {
                    method: 'GET'
                })
                .then(response => {
                    return response.json();
                })
                .then(deviceDataList => {
                    if (Array.isArray(deviceDataList) && deviceDataList.length > 0) {
                        deviceDataList.forEach((item) => {
                            const deviceName = item.device_name;
                            const temperature = item.temperature;
                            const humidity = item.humidity;
                            updateData(deviceName, temperature, humidity);
                        });
                    }
                })
                .catch(error => {
                    console.error('请求出错:', error);
                });
            }
            function updateData(deviceName, temperature, humidity){
                    const deviceItem = document.getElementById(deviceName);
                    if (!deviceItem) return;
		    deviceItem.querySelectorAll('p')[0].innerText = `箱内温度: ${temperature === null || temperature === undefined ? '--' : temperature}`;
                    deviceItem.querySelectorAll('p')[1].innerText = `箱内湿度: ${humidity === null || humidity === undefined ? '--' : humidity}`;
                    
            }
            addDeviceBtn.addEventListener('click', () => {
                deviceModal.style.display = 'flex';
                deviceNameInput.focus();
            });
            btnClose.addEventListener('click', () => {
                deviceModal.style.display = 'none';
            });
            deviceModal.addEventListener('click', (e) => {
                if (e.target === deviceModal) {
                    deviceModal.style.display = 'none';
                }
            });
            deviceForm.addEventListener('submit', function(e) {
                e.preventDefault(); // 阻止页面刷新
            
                    //获得表单输入的设备名
                    const deviceName = deviceNameInput.value.trim();
                    if (!deviceName) return;

                    // 新后端: 注册设备, api_key 未提供时自动生成并返回
                    fetch('/api/v1/devices', {
                        method: 'POST',
                        headers:{
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({
                            device_name: deviceName
                        })
                    })
                    .then(res => {
                        return res.json();
                    })
                    .then(data => {
                        if(data.success){
                            const newDeviceElement = createDeviceElement(deviceName, null, null);
                            deviceList.appendChild(newDeviceElement);
                            deviceNameInput.value = '';
                            deviceModal.style.display = 'none';
                            const apiKey = data.data && data.data.api_key;
                            alert(`设备 "${deviceName}" 添加成功！` + (apiKey ? `\n设备 API Key (请配置到设备端):\n${apiKey}` : ''));
                        }else{
                            alert(`设备添加失败\n${data.message || data.detail || ''}`)
                        }
                    })
                    .catch(error => {
                        console.error('添加设备出错:', error);
                        alert('设备添加失败');
                    });
            })
            });
