package com.sourcegraph.demo.app

import com.sourcegraph.demo.app.controller.PetController
import io.ktor.features.*
import com.github.trly.utils.Printer
import io.ktor.application.*
import io.ktor.http.*
import io.ktor.jackson.*
import io.ktor.routing.*
import io.ktor.server.engine.*
import io.ktor.server.netty.*
import com.fasterxml.jackson.databind.SerializationFeature
import com.sourcegraph.demo.app.service.PetService

fun main() {
    val printer = Printer("Starting Pet Store API Server...")
    printer.printMessage()

    // Initialize the service
    val petService = PetService()
    val petController = PetController(petService)

    // Start Ktor server
    embeddedServer(Netty, port = 8080) {
        install(ContentNegotiation) {
            jackson {
                enable(SerializationFeature.INDENT_OUTPUT)
            }
        }

        install(CORS) {
            method(HttpMethod.Options)
            method(HttpMethod.Get)
            header(HttpHeaders.AccessControlAllowOrigin)
            header(HttpHeaders.ContentType)
            anyHost()
        }

        routing {
            petController.setupRoutes(this)
        }
    }.start(wait = true)
}
