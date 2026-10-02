// ============================================================
// N8N_FUNCTIONS.js
// Mika n8n Playwright Controller
//
// Purpose:
// - Give Mika visible control over n8n UI
// - Add nodes
// - Explain nodes
// - Connect workflows
// - Add notes
// - Configure nodes
// - Control canvas
//
// Requires:
// playwright
//
// Usage:
//
// const { N8nController } = require("./N8N_FUNCTIONS");
//
// const n8n = new N8nController(page);
//
// await n8n.waitForCanvas();
// await n8n.addNode("Schedule Trigger");
//
// ============================================================



class N8nController {


    constructor(page, options = {}) {

        this.page = page;


        this.options = {

            typingDelay:
                options.typingDelay ?? 40,

            actionDelay:
                options.actionDelay ?? 500,

            teachingMode:
                options.teachingMode ?? true,

            debug:
                options.debug ?? false

        };


        this.SELECTORS = {


            // ------------------------------
            // CANVAS
            // ------------------------------

            canvas:
                '[data-test-id="canvas"]',


            canvasWrapper:
                '[data-test-id="canvas-wrapper"]',


            firstStep:
                '[data-test-id="canvas-add-first-step-button"]',


            plusButton:
                '[data-test-id="node-creator-plus-button"]',



            // ------------------------------
            // NODE CREATOR
            // ------------------------------

            nodeCreator:
                '[data-test-id="node-creator"]',


            nodeSearch:
                '[data-test-id="node-creator-search-bar"]',


            nodeItem:
                '[data-test-id="node-creator-node-item"]',


            nodeName:
                '[data-test-id="node-creator-item-name"]',



            // ------------------------------
            // NODES
            // ------------------------------

            canvasNode:
                '[data-test-id="canvas-node"]',


            nodeOutput:
                '[data-test-id="canvas-node-output-handle"]',


            nodeInput:
                '[data-test-id="canvas-node-input-handle"]',



            // ------------------------------
            // NODE DETAILS
            // ------------------------------

            ndv:
                '[data-test-id="ndv"]',


            closeNDV:
                '[data-test-id="ndv-close-button"]',


            parameterInput:
                '[data-test-id="parameter-input"]',



            // ------------------------------
            // NOTES
            // ------------------------------

            addSticky:
                '[data-test-id="add-sticky-button"]',



            // ------------------------------
            // CANVAS TOOLS
            // ------------------------------

            zoomFit:
                '[data-test-id="zoom-to-fit"]',


            zoomIn:
                '[data-test-id="zoom-in-button"]',


            zoomOut:
                '[data-test-id="zoom-out-button"]',


            tidy:
                '[data-test-id="tidy-up-button"]',



            // ------------------------------
            // EXECUTION
            // ------------------------------

            executeWorkflow:
                '[data-test-id="execute-workflow-button"]',


            stopExecution:
                '[data-test-id="stop-execution-button"]',



            // ------------------------------
            // WORKFLOW
            // ------------------------------

            workflowName:
                '[data-test-id="workflow-name-input"]',


            publish:
                '[data-test-id="workflow-publish-button"]'


        };


    }



    // ========================================================
    // INTERNAL HELPERS
    // ========================================================


    async sleep(ms = null) {

        await this.page.waitForTimeout(
            ms ?? this.options.actionDelay
        );

    }



    async log(...args) {

        if(this.options.debug) {

            console.log(
                "[N8N]",
                ...args
            );

        }

    }



    async click(selector) {

        await this.page
            .locator(selector)
            .click();

        await this.sleep(this.SPEED?.actionDelay || 500);

    }



    async visible(selector) {

        return await this.page
            .locator(selector)
            .isVisible()
            .catch(()=>false);

    }



    async exists(selector) {

        return (
            await this.page
                .locator(selector)
                .count()
        ) > 0;

    }



    async type(selector,text) {


        await this.page
            .locator(selector)
            .fill("");


        await this.page
            .locator(selector)
            .pressSequentially(
                text,
                {
                    delay:
                    this.options.typingDelay
                }
            );


    }



    // ========================================================
    // WAIT FOR N8N
    // ========================================================


    async waitForCanvas() {


        await this.page
            .locator(
                this.SELECTORS.canvas
            )
            .waitFor({

                state:"visible",

                timeout:60000

            });


        await this.sleep(1000);


        await this.log(
            "Canvas ready"
        );


    }



    async waitForNode(name) {


        await this.page.waitForFunction(

            (nodeName)=>{

                return [
                    ...document.querySelectorAll(
                        '[data-test-id="canvas-node"]'
                    )
                ].some(
                    n =>
                    n.innerText.includes(nodeName)
                );

            },

            name,

            {
                timeout:30000
            }

        );


        return true;


    }



    // ========================================================
    // NODE CREATOR
    // ========================================================


    async openNodeCreator() {


        if(
            await this.visible(
                this.SELECTORS.firstStep
            )
        ){

            await this.click(
                this.SELECTORS.firstStep
            );

        }
        else {


            await this.click(
                this.SELECTORS.plusButton
            );


        }


        await this.page
            .locator(
                this.SELECTORS.nodeCreator
            )
            .waitFor({

                state:"visible",

                timeout:30000

            });


        await this.sleep(this.SPEED?.actionDelay || 500);


    }



    async searchNode(name) {


        await this.type(
            this.SELECTORS.nodeSearch,
            name
        );


        await this.sleep(1000);



    }





    async waitForNodeCreatorClose(){

        await this.page.waitForFunction(()=>{

            return !document.querySelector(
                '[data-test-id="node-creator"]'
            );

        },{
            timeout:30000
        });

    }




    async waitForNodeCreated(name){

        await this.page.waitForFunction(

            (nodeName)=>{

                return [
                    ...document.querySelectorAll(
                        '[data-test-id="canvas-node"]'
                    )
                ]
                .some(
                    el =>
                    el.innerText.includes(nodeName)
                );

            },

            name,

            {
                timeout:30000
            }

        );

    }


    async selectNode(name) {


        const node =
            this.page
            .locator(
                this.SELECTORS.nodeName
            )
            .filter({

                hasText:name

            })
            .first();



        await node.click({
            force:true
        });



        await this.sleep(2000);


    }


    // ========================================================
    // ADD NODES
    // ========================================================


    async addNode(name, options = {}) {


        await this.log(
            "Adding node:",
            name
        );


        await this.openNodeCreator();



        await this.searchNode(
            name
        );



        const item =
            this.page
            .locator(
                '[data-test-id="node-creator-node-item"]'
            )
            .filter({
                hasText:name
            })
            .first();


        await item.waitFor({
            state:"visible",
            timeout:30000
        });


        await item.click();


        await this.waitForNodeCreated(
            name
        );


        await this.sleep(200);



        await this.sleep(this.SPEED?.actionDelay || 500);



        if(options.open !== false){

            await this.openNode(
                name
            );

        }



        return true;


    }



    async addNodeFrom(
        sourceNode,
        targetNode
    ){

        await this.log(
            "Adding node from:",
            sourceNode,
            "→",
            targetNode
        );



        const source =

            this.page.locator(

                `${this.SELECTORS.canvasNode}` +
                `[data-node-name="${sourceNode}"]`

            );



        await source.hover();



        const plus =

            this.page.locator(

                `${this.SELECTORS.nodeOutput}` +
                `[data-node-name="${sourceNode}"]` +
                ` [data-test-id="canvas-handle-plus"]`

            ).first();



        if(
            await plus.count()
        ){

            await plus.click();

        }

        else {


            // fallback:
            // open creator manually

            await this.openNodeCreator();


        }



        await this.page
            .locator(
                this.SELECTORS.nodeCreator
            )
            .waitFor({

                state:"visible",

                timeout:30000

            });



        await this.searchNode(
            targetNode
        );



        await this.selectNode(
            targetNode
        );



        await this.waitForNode(
            targetNode
        );



        return true;


    }





    // ========================================================
    // FIND / SELECT NODES
    // ========================================================



    node(name){


        return this.page.locator(

            `${this.SELECTORS.canvasNode}` +
            `[data-node-name="${name}"]`

        );


    }



    async selectNode(name){


        const node =
            this.node(name);



        await node.click({
            force:true
        });



        await this.sleep(this.SPEED?.actionDelay || 500);



    }




    async hoverNode(name){


        await this.node(name)
            .hover();



        await this.sleep(this.SPEED?.actionDelay || 500);



    }




    // ========================================================
    // OPEN / CLOSE NODE SETTINGS
    // ========================================================



    async openNode(name){


        await this.log(
            "Opening node:",
            name
        );



        await this.node(name)
            .dblclick();



        await this.page.waitForTimeout(1000);


        const ndv =
            this.page.locator(
                this.SELECTORS.ndv
            );


        if(await ndv.count()){

            await ndv.waitFor({

                state:"visible",

                timeout:5000

            }).catch(()=>{});

        }



        await this.sleep(this.SPEED?.actionDelay || 500);



    }





    async closeNode(){


        if(
            await this.visible(
                this.SELECTORS.closeNDV
            )
        ){

            await this.click(
                this.SELECTORS.closeNDV
            );

        }



    }





    async getOpenNodeTitle(){


        return await this.page
            .locator(
                this.SELECTORS.ndv
            )
            .innerText()
            .catch(()=>null);


    }





    // ========================================================
    // CONNECTIONS
    // ========================================================



    async connectNodes(
        source,
        target
    ){


        await this.log(
            "Connecting:",
            source,
            "→",
            target
        );



        const output =

            this.page.locator(

                `${this.SELECTORS.nodeOutput}` +
                `[data-node-name="${source}"]`

            ).first();



        const input =

            this.page.locator(

                `${this.SELECTORS.nodeInput}` +
                `[data-node-name="${target}"]`

            ).first();



        await output.dragTo(
            input
        );



        await this.sleep(
            300
        );



    }





    async hasConnection(){


        return (
            await this.page
                .locator(
                    '[data-test-id="edge"]'
                )
                .count()
        ) > 0;


    }




    async connectionCount(){


        return await this.page
            .locator(
                '[data-test-id="edge"]'
            )
            .count();


    }





    // ========================================================
    // DELETE NODES
    // ========================================================



    async deleteNode(name){


        await this.log(
            "Deleting:",
            name
        );



        const node =
            this.node(name);



        await node.click({
            force:true
        });



        await this.page.keyboard.press(
            "Delete"
        );



        await this.sleep(
            1000
        );


    }





    async disableNode(name){


        await this.node(name)
            .click();



        await this.sleep(this.SPEED?.actionDelay || 500);



        const button =

            this.page.locator(

                '[data-test-id="disable-node-button"]'

            );



        if(
            await button.count()
        ){

            await button.click();

        }



    }





    // ========================================================
    // NODE MOVEMENT
    // ========================================================



    async moveNode(
        name,
        x,
        y
    ){


        const node =
            this.node(name);



        const box =
            await node.boundingBox();



        if(!box){

            throw new Error(
                "Node not found: "+name
            );

        }



        await this.page.mouse.move(

            box.x + box.width/2,

            box.y + box.height/2

        );



        await this.page.mouse.down();



        await this.page.mouse.move(

            x,

            y,

            {
                steps:20
            }

        );



        await this.page.mouse.up();



        await this.sleep(this.SPEED?.actionDelay || 500);



    }
    // ========================================================
    // STICKY NOTES
    // ========================================================


    async addSticky(text){

        await this.log(
            "Adding sticky note:",
            text
        );


        await this.click(
            this.SELECTORS.addSticky
        );


        await this.sleep(
            1000
        );


        // Find editable sticky area
        const editable =
            this.page.locator(
                '[contenteditable="true"]'
            ).last();



        if(
            await editable.count()
        ){

            await editable.fill(
                text
            );

        }


        await this.sleep(this.SPEED?.actionDelay || 500);



        return true;


    }





    async editSticky(
        text
    ){

        const editable =
            this.page.locator(
                '[contenteditable="true"]'
            ).last();



        await editable.click();



        await editable.fill(
            text
        );



        await this.sleep(this.SPEED?.actionDelay || 500);



    }





    async moveSticky(
        fromX,
        fromY,
        toX,
        toY
    ){


        await this.page.mouse.move(
            fromX,
            fromY
        );


        await this.page.mouse.down();



        await this.page.mouse.move(
            toX,
            toY,
            {
                steps:20
            }
        );


        await this.page.mouse.up();



        await this.sleep(this.SPEED?.actionDelay || 500);



    }





    async resizeSticky(
        x,
        y
    ){

        // Generic resize because sticky notes
        // use canvas handles

        await this.page.mouse.move(
            x,
            y
        );


        await this.page.mouse.down();



        await this.page.mouse.move(
            x + 100,
            y + 100,
            {
                steps:20
            }
        );


        await this.page.mouse.up();



    }





    // ========================================================
    // CANVAS CONTROL
    // ========================================================



    async zoomFit(){


        await this.log(
            "Zoom fit"
        );


        await this.click(
            this.SELECTORS.zoomFit
        );


    }




    async zoomIn(){


        await this.click(
            this.SELECTORS.zoomIn
        );


    }




    async zoomOut(){


        await this.click(
            this.SELECTORS.zoomOut
        );


    }




    async tidyUp(){


        await this.log(
            "Tidying canvas"
        );


        await this.click(
            this.SELECTORS.tidy
        );


        await this.sleep(
            300
        );


    }





    async panCanvas(
        fromX,
        fromY,
        toX,
        toY
    ){


        await this.page.mouse.move(
            fromX,
            fromY
        );


        await this.page.mouse.down();



        await this.page.mouse.move(
            toX,
            toY,
            {
                steps:20
            }
        );


        await this.page.mouse.up();



        await this.sleep(this.SPEED?.actionDelay || 500);



    }





    // ========================================================
    // WORKFLOW MANAGEMENT
    // ========================================================





    async setSpeed(mode="normal"){

        const speeds = {

            instant:{
                actionDelay:50,
                typingDelay:5,
                nodeDelay:100
            },

            normal:{
                actionDelay:300,
                typingDelay:20,
                nodeDelay:500
            },

            teaching:{
                actionDelay:1200,
                typingDelay:50,
                nodeDelay:2000
            }

        };


        this.SPEED =
        speeds[mode] || speeds.normal;


        await this.log(
            "Speed mode:",
            mode
        );


    }


    async visualPause(ms){

        await this.page.waitForTimeout(
            ms
        );

    }



    async renameWorkflow(
        name
    ){


        await this.log(
            "Rename workflow:",
            name
        );


        const title =
            this.page.locator(
                '[data-test-id="workflow-name-input"]'
            );


        await title.waitFor({
            state:"visible",
            timeout:30000
        });


        await title.click();


        const input =
            this.page.locator(
                '[data-test-id="inline-edit-input"]'
            );


        await input.waitFor({
            state:"visible",
            timeout:10000
        });


        await input.fill(
            name
        );


        await input.press(
            "Enter"
        );


        await this.page.waitForFunction(
            (expected)=>{

                const el =
                document.querySelector(
                    '[data-test-id="workflow-name-input"]'
                );

                return el &&
                el.innerText.includes(expected);

            },
            name,
            {
                timeout:10000
            }
        );


        await this.log(
            "Workflow renamed"
        );


    }



    async publish(){


        await this.log(
            "Publishing workflow"
        );


        await this.click(
            this.SELECTORS.publish
        );


        await this.sleep(
            3000
        );


    }





    async save(){


        await this.page.keyboard.press(
            "Control+s"
        );


        await this.sleep(
            1000
        );


    }





    // ========================================================
    // EXECUTION
    // ========================================================



    async executeWorkflow(){


        await this.log(
            "Execute workflow"
        );



        await this.click(
            this.SELECTORS.executeWorkflow
        );


        await this.sleep(
            5000
        );


    }





    async stopExecution(){


        if(
            await this.visible(
                this.SELECTORS.stopExecution
            )
        ){

            await this.click(
                this.SELECTORS.stopExecution
            );

        }


    }





    async waitForExecution(
        timeout = 60000
    ){


        await this.page.waitForTimeout(
            timeout
        );


    }





    // ========================================================
    // NODE PARAMETERS
    // ========================================================



    async fillParameter(
        label,
        value
    ){


        const field =
            this.page
            .locator(
                this.SELECTORS.parameterInput
            )
            .filter({

                hasText:
                    label

            })
            .first();



        await field.fill(
            value
        );



        await this.sleep(this.SPEED?.actionDelay || 500);



    }





    async fillInput(
        selector,
        value
    ){


        await this.page
            .locator(selector)
            .fill(
                value
            );



    }





    async selectOption(
        selector,
        value
    ){


        await this.page
            .locator(selector)
            .selectOption(
                value
            );


    }
    // ========================================================
    // CREDENTIALS
    // ========================================================



    async openCredentialSelector(){


        const selector =

            this.page.locator(

                '[data-test-id="credential-select"]'

            );



        if(
            await selector.count()
        ){

            await selector.click();

            await this.sleep(this.SPEED?.actionDelay || 500);

        }



    }





    async chooseCredential(
        name
    ){


        await this.page
            .getByText(
                name,
                {
                    exact:true
                }
            )
            .click();



        await this.sleep(this.SPEED?.actionDelay || 500);



    }





    async saveCredentials(){


        const save =

            this.page.locator(

                '[data-test-id="credential-save-button"]'

            );



        if(
            await save.count()
        ){

            await save.click();

        }



        await this.sleep(this.SPEED?.actionDelay || 500);



    }





    // ========================================================
    // EXPRESSIONS
    // ========================================================



    async enableExpression(
        selector
    ){


        const input =
            this.page.locator(
                selector
            );



        await input.click();



        await this.page.keyboard.press(
            "Control+k"
        );



        await this.sleep(this.SPEED?.actionDelay || 500);



    }





    async setExpression(
        selector,
        expression
    ){


        await this.page
            .locator(selector)
            .fill(
                expression
            );



        await this.sleep(this.SPEED?.actionDelay || 500);



    }





    // ========================================================
    // KEYBOARD CONTROLS
    // ========================================================



    async press(
        key
    ){


        await this.page.keyboard.press(
            key
        );


        await this.sleep(this.SPEED?.actionDelay || 500);



    }





    async deleteSelected(){


        await this.page.keyboard.press(
            "Delete"
        );


        await this.sleep(this.SPEED?.actionDelay || 500);



    }





    async duplicateSelected(){


        await this.page.keyboard.press(
            "Control+d"
        );


        await this.sleep(this.SPEED?.actionDelay || 500);



    }





    async undo(){


        await this.page.keyboard.press(
            "Control+z"
        );


    }





    async redo(){


        await this.page.keyboard.press(
            "Control+Shift+z"
        );


    }





    // ========================================================
    // CONTEXT MENU
    // ========================================================



    async rightClickNode(
        name
    ){


        await this.node(name)
            .click({

                button:"right"

            });



        await this.sleep(this.SPEED?.actionDelay || 500);



    }





    async clickContextItem(
        text
    ){


        await this.page
            .getByText(
                text,
                {
                    exact:true
                }
            )
            .click();



        await this.sleep(this.SPEED?.actionDelay || 500);



    }





    // ========================================================
    // DEBUG / DOM INSPECTION
    // ========================================================



    async dumpTestIds(){


        const ids =

            await this.page.evaluate(()=>{


                return [

                    ...document
                    .querySelectorAll(
                        "[data-test-id]"
                    )

                ]
                .map(
                    e =>
                    e.getAttribute(
                        "data-test-id"
                    )
                )
                .filter(Boolean);



            });



        console.log(
            JSON.stringify(
                [
                    ...new Set(ids)
                ],
                null,
                2
            )
        );


        return ids;


    }





    async dumpButtons(){


        const buttons =

            await this.page.evaluate(()=>{


                return [

                    ...document
                    .querySelectorAll(
                        "button"
                    )

                ]
                .map(
                    b =>
                    ({

                        text:
                        b.innerText,

                        testId:
                        b.dataset.testId,

                        aria:
                        b.getAttribute(
                            "aria-label"
                        )

                    })

                );


            });



        console.log(
            JSON.stringify(
                buttons,
                null,
                2
            )
        );


        return buttons;


    }





    async dumpInputs(){


        const inputs =

            await this.page.evaluate(()=>{


                return [

                    ...document
                    .querySelectorAll(
                        "input"
                    )

                ]
                .map(

                    i =>
                    ({

                        type:
                        i.type,

                        value:
                        i.value,

                        placeholder:
                        i.placeholder,

                        testId:
                        i.dataset.testId

                    })

                );


            });



        console.log(
            JSON.stringify(
                inputs,
                null,
                2
            )
        );


        return inputs;


    }





    async screenshot(
        path="n8n_debug.png"
    ){


        await this.page.screenshot({

            path,

            fullPage:false

        });


    }





    async inspectCanvas(){


        const data =

            await this.page.evaluate(()=>{


                const nodes =

                    [

                        ...document
                        .querySelectorAll(
                            '[data-test-id="canvas-node"]'
                        )

                    ]
                    .map(

                        n =>

                        ({

                            name:
                            n.dataset.nodeName,

                            text:
                            n.innerText

                        })

                    );



                const edges =

                    document
                    .querySelectorAll(
                        '[data-test-id="edge"]'
                    )
                    .length;



                return {

                    nodes,

                    edges

                };


            });



        console.log(
            JSON.stringify(
                data,
                null,
                2
            )
        );


        return data;


    }





    // ========================================================
    // WAIT HELPERS
    // ========================================================



    async waitForText(
        text
    ){


        await this.page
            .getByText(
                text
            )
            .waitFor({

                state:"visible",

                timeout:30000

            });


    }





    async waitForSelector(
        selector
    ){


        await this.page
            .locator(selector)
            .waitFor({

                state:"visible",

                timeout:30000

            });


    }
    // ========================================================
    // MIKA TEACHING ACTIONS
    //
    // These are the high-level functions.
    //
    // Mika does not think:
    // "click selector xyz"
    //
    // Mika thinks:
    // "Teach Schedule Trigger"
    //
    // These functions translate that.
    // ========================================================





    async teachAddNode(
        nodeName,
        explanation = null
    ){


        await this.log(
            "Teaching add node:",
            nodeName
        );



        // Add node visually

        await this.addNode(
            nodeName,
            {
                open:true
            }
        );



        // Optional pause
        // while Mika talks

        if(explanation){

            await this.teachingPause(
                explanation
            );

        }



        return true;


    }





    async teachConnect(
        from,
        to,
        explanation = null
    ){


        await this.log(
            "Teaching connection:",
            from,
            "→",
            to
        );



        await this.connectNodes(
            from,
            to
        );



        if(explanation){

            await this.teachingPause(
                explanation
            );

        }



    }





    async teachExplainNode(
        nodeName
    ){


        await this.log(
            "Explaining:",
            nodeName
        );



        await this.openNode(
            nodeName
        );



        return true;


    }





    async teachingPause(
        message
    ){


        /*
        
        Placeholder.

        Later this connects to:

        Mika voice:
            await voice.say(message)

        VR avatar:
            animation

        Eye tracking:
            look at node


        For now:
        console output.

        */


        console.log(
            "\nMIKA:",
            message,
            "\n"
        );


        await this.sleep(
            3000
        );


    }





    // ========================================================
    // COMPLETE LESSON EXAMPLE
    //
    // Mika:
    //
    // "Let's build an automation."
    //
    // ========================================================



    async buildScheduleToGmailLesson(){



        await this.teachingPause(

            "Every automation starts with a trigger."

        );



        await this.teachAddNode(

            "Schedule Trigger",

            "This node starts our workflow automatically."

        );



        await this.closeNode();



        await this.teachingPause(

            "Now we need something to happen after the trigger."

        );



        await this.addNodeFrom(

            "Schedule Trigger",

            "Gmail"

        );



        await this.closeNode();



        await this.teachingPause(

            "Now the schedule trigger sends information to Gmail."

        );



        await this.teachConnect(

            "Schedule Trigger",

            "Gmail"

        );



        await this.tidyUp();



        await this.zoomFit();



        await this.teachingPause(

            "Our first automation is complete."

        );



    }





    // ========================================================
    // ACTION DISPATCHER
    //
    // Allows AI planner to call:
    //
    // {
    //    action:"addNode",
    //    args:{
    //       name:"Gmail"
    //    }
    // }
    //
    // ========================================================



    async perform(
        action,
        args={}
    ){



        switch(action){



            case "addNode":

                return await this.addNode(
                    args.name,
                    args.options || {}
                );



            case "addNodeFrom":

                return await this.addNodeFrom(

                    args.from,

                    args.to

                );



            case "connect":

                return await this.connectNodes(

                    args.from,

                    args.to

                );



            case "deleteNode":

                return await this.deleteNode(

                    args.name

                );



            case "openNode":

                return await this.openNode(

                    args.name

                );



            case "closeNode":

                return await this.closeNode();



            case "addSticky":

                return await this.addSticky(

                    args.text

                );



            case "editSticky":

                return await this.editSticky(

                    args.text

                );



            case "tidyUp":

                return await this.tidyUp();



            case "zoomFit":

                return await this.zoomFit();



            case "execute":

                return await this.executeWorkflow();



            case "save":

                return await this.save();



            case "publish":

                return await this.publish();



            case "rename":

                return await this.renameWorkflow(

                    args.name

                );



            case "inspect":

                return await this.inspectCanvas();



            case "screenshot":

                return await this.screenshot(

                    args.path

                );



            default:


                throw new Error(

                    "Unknown Mika action: " +
                    action

                );


        }


    }





    // ========================================================
    // AUTOMATION RECIPES
    //
    // Prebuilt teaching patterns.
    //
    // ========================================================



    async createBasicAutomation(
        trigger,
        action
    ){


        await this.teachingPause(

            "Let's create an automation."

        );



        await this.addNode(
            trigger
        );



        await this.closeNode();



        await this.teachingPause(

            "Now let's add the action."

        );



        await this.addNodeFrom(

            trigger,

            action

        );



        await this.closeNode();



        await this.tidyUp();



        await this.zoomFit();



    }





    async explainWorkflow(){


        const state =
            await this.inspectCanvas();



        console.log(

            "Workflow contains:",

            state.nodes.length,

            "nodes"

        );



        for(
            const node of state.nodes
        ){

            console.log(

                "Node:",
                node.name

            );

        }



    }
    // ========================================================
    // ADVANCED NODE INSPECTION
    // ========================================================


    async getNodeData(name){


        const node =
            this.node(name);



        if(
            !(await node.count())
        ){

            return null;

        }



        return await node.evaluate(
            el => ({


                name:
                el.dataset.nodeName,


                text:
                el.innerText,


                classes:
                el.className


            })
        );


    }





    async countNodes(){


        return await this.page
            .locator(
                this.SELECTORS.canvasNode
            )
            .count();


    }





    async listNodes(){


        return await this.page.evaluate(()=>{


            return [

                ...document.querySelectorAll(
                    '[data-test-id="canvas-node"]'
                )

            ]

            .map(
                node => ({

                    name:
                    node.dataset.nodeName,

                    text:
                    node.innerText

                })

            );


        });


    }





    // ========================================================
    // MIKA CLASSROOM HELPERS
    //
    // Used for VR teaching flow
    // ========================================================



    async focusNode(name){


        await this.hoverNode(
            name
        );


        await this.sleep(this.SPEED?.actionDelay || 500);



    }





    async highlightNode(name){


        const node =
            this.node(name);



        await node.click({
            force:true
        });



        await this.sleep(this.SPEED?.actionDelay || 500);



    }





    async showWorkflow(){


        await this.zoomFit();



        await this.sleep(
            300
        );


    }





    async resetView(){


        await this.zoomFit();



        await this.sleep(this.SPEED?.actionDelay || 500);



    }





    // ========================================================
    // WAIT FOR UI CHANGES
    // ========================================================



    async waitForNodeDisappear(
        name
    ){


        await this.page
            .locator(

                `${this.SELECTORS.canvasNode}` +
                `[data-node-name="${name}"]`

            )
            .waitFor({

                state:"hidden",

                timeout:30000

            });



    }





    async waitForNodeCount(
        count
    ){


        await this.page.waitForFunction(

            (expected)=>{


                return document.querySelectorAll(

                    '[data-test-id="canvas-node"]'

                )
                .length === expected;


            },

            count,

            {
                timeout:30000
            }

        );


    }





    // ========================================================
    // GENERIC UI CONTROL
    //
    // Escape hatch for future n8n updates
    // ========================================================



    async clickText(
        text
    ){


        await this.page
            .getByText(

                text,

                {
                    exact:true
                }

            )
            .click();



        await this.sleep(this.SPEED?.actionDelay || 500);



    }





    async fillByLabel(
        label,
        value
    ){


        await this.page
            .getByLabel(
                label
            )
            .fill(
                value
            );



    }





    async clickTestId(
        id
    ){


        await this.page
            .locator(

                `[data-test-id="${id}"]`

            )
            .click();



        await this.sleep(this.SPEED?.actionDelay || 500);



    }





    async evaluate(
        fn
    ){


        return await this.page.evaluate(
            fn
        );


    }





    // ========================================================
    // END CONTROLLER
    // ========================================================


}



// ============================================================
// EXPORT
// ============================================================


module.exports = {

    N8nController

};



// ============================================================
// EXAMPLE:
//
// const {N8nController} = require("./N8N_FUNCTIONS");
//
// const n8n = new N8nController(page);
//
// await n8n.waitForCanvas();
//
// await n8n.buildScheduleToGmailLesson();
//
// ============================================================
