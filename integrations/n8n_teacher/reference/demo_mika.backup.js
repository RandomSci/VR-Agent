const { chromium } = require("playwright");
const http = require("http");

const { N8nMCP } = require("./n8n_mcp");


function getChrome(){

    return new Promise((resolve,reject)=>{

        http.get(
            "http://127.0.0.1:9222/json/version",
            res=>{

                let data="";

                res.on("data",c=>data+=c);

                res.on("end",()=>{

                    resolve(
                        JSON.parse(data)
                        .webSocketDebuggerUrl
                    );

                });

            }
        ).on("error",reject);

    });

}



async function connectBrowser(){

    const ws =
    await getChrome();


    const browser =
    await chromium.connectOverCDP(ws);


    const page =
    browser.contexts()[0]
    .pages()[0];


    await page.goto(
        "https://myaisupport.app.n8n.cloud/projects/SQVrZFuPWfa7zE1N/workflows",
        {
            waitUntil:"domcontentloaded"
        }
    );


    await page.waitForSelector(
        '[data-test-id="project-name"]'
    );


    return page;

}



const scheduleCode = `

const schedule = trigger({

type:'n8n-nodes-base.scheduleTrigger',

version:1.3,

config:{

name:'Schedule Trigger',

parameters:{},

position:[300,300]

},

output:[{}]

});


export default workflow(
'mika-live-demo',
'Mika Live Demo'
)

.add(schedule);

`;



async function main(){


const page =
await connectBrowser();


const mcp =
new N8nMCP();


await mcp.connect();



console.log(
"Mika: Welcome. Let's build an automation."
);


await page.waitForTimeout(
5000
);



console.log(
"Mika: First, we add a Schedule Trigger."
);



const created =
await mcp.createWorkflowSDK(
scheduleCode
);



const data =
JSON.parse(
created.content[0].text
);



console.log(
"Workflow:",
data.workflowId
);



await page.goto(
data.url,
{
waitUntil:"domcontentloaded"
}
);



await page.waitForSelector(
'[data-test-id="canvas"]'
);



console.log(
"Mika: This trigger decides when the automation starts."
);


await page.waitForTimeout(
10000
);


console.log(
"Mika: Now I will add the Code node live."
);



await page.waitForTimeout(
10000
);



console.log(
"Mika: Now adding Code node live..."
);



const updateResult =
await mcp.updateWorkflow(
data.workflowId,
[
{
type:"addNode",
node:{

name:"Code",

type:"n8n-nodes-base.code",

typeVersion:2,

position:[
600,
300
],

parameters:{

jsCode:
"return [{json:{message:'automation successful'}}];"

}

}

},
{
type:"addConnection",

source:"Schedule Trigger",

target:"Code"

}

]
);



console.log(
"UPDATE RESPONSE:"
);


console.log(
JSON.stringify(
updateResult,
null,
2
)
);


console.log(
"Mika: Code node added."
);


await page.waitForTimeout(3000);


console.log(
"Mika: Now I will execute the automation."
);


const execution =
await mcp.executeWorkflow(
data.workflowId
);


console.log(
"EXECUTION RESPONSE:"
);


console.log(
JSON.stringify(
execution,
null,
2
)
);



await page.waitForTimeout(
15000
);



console.log(
"Mika: Opening Code node..."
);



}

main()
.catch(console.error);
